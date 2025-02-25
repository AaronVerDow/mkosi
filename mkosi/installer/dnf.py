# SPDX-License-Identifier: LGPL-2.1+
import os
import textwrap
from collections.abc import Iterable, Sequence
from pathlib import Path

from mkosi.config import Cacheonly, Config
from mkosi.context import Context
from mkosi.installer import PackageManager
from mkosi.installer.rpm import RpmRepository, rpm_cmd
from mkosi.log import ARG_DEBUG
from mkosi.mounts import finalize_source_mounts
from mkosi.run import find_binary, run
from mkosi.sandbox import apivfs_cmd
from mkosi.types import _FILE, CompletedProcess, PathString


class Dnf(PackageManager):
    @classmethod
    def executable(cls, config: Config) -> str:
        # Allow the user to override autodetection with an environment variable
        dnf = config.environment.get("MKOSI_DNF")
        root = config.tools()

        return Path(dnf or find_binary("dnf5", root=root) or find_binary("dnf", root=root) or "yum").name

    @classmethod
    def subdir(cls, config: Config) -> Path:
        return Path("libdnf5" if cls.executable(config) == "dnf5" else "dnf")

    @classmethod
    def cache_subdirs(cls, cache: Path) -> list[Path]:
        return [
            p / "packages"
            for p in cache.iterdir()
            if p.is_dir() and "-" in p.name and "mkosi" not in p.name
        ]

    @classmethod
    def scripts(cls, context: Context) -> dict[str, list[PathString]]:
        return {
            "dnf": apivfs_cmd(context.root) + cls.cmd(context),
            "rpm": apivfs_cmd(context.root) + rpm_cmd(context),
            "mkosi-install"  : ["dnf", "install"],
            "mkosi-upgrade"  : ["dnf", "upgrade"],
            "mkosi-remove"   : ["dnf", "remove"],
            "mkosi-reinstall": ["dnf", "reinstall"],
        }

    @classmethod
    def setup(cls, context: Context, repositories: Iterable[RpmRepository], filelists: bool = True) -> None:
        (context.pkgmngr / "etc/dnf/vars").mkdir(parents=True, exist_ok=True)
        (context.pkgmngr / "etc/yum.repos.d").mkdir(parents=True, exist_ok=True)

        config = context.pkgmngr / "etc/dnf/dnf.conf"

        if not config.exists():
            config.parent.mkdir(exist_ok=True, parents=True)
            with config.open("w") as f:
                # Make sure we download filelists so all dependencies can be resolved.
                # See https://bugzilla.redhat.com/show_bug.cgi?id=2180842
                if cls.executable(context.config).endswith("dnf5") and filelists:
                    f.write("[main]\noptional_metadata_types=filelists\n")

        repofile = context.pkgmngr / "etc/yum.repos.d/mkosi.repo"
        if not repofile.exists():
            repofile.parent.mkdir(exist_ok=True, parents=True)
            with repofile.open("w") as f:
                for repo in repositories:
                    f.write(
                        textwrap.dedent(
                            f"""\
                            [{repo.id}]
                            name={repo.id}
                            {repo.url}
                            gpgcheck=1
                            enabled={int(repo.enabled)}
                            """
                        )
                    )

                    if repo.sslcacert:
                        f.write(f"sslcacert={repo.sslcacert}\n")
                    if repo.sslclientcert:
                        f.write(f"sslclientcert={repo.sslclientcert}\n")
                    if repo.sslclientkey:
                        f.write(f"sslclientkey={repo.sslclientkey}\n")
                    if repo.priority:
                        f.write(f"priority={repo.priority}\n")

                    for i, url in enumerate(repo.gpgurls):
                        f.write("gpgkey=" if i == 0 else len("gpgkey=") * " ")
                        f.write(f"{url}\n")

                    f.write("\n")

    @classmethod
    def cmd(cls, context: Context) -> list[PathString]:
        dnf = cls.executable(context.config)

        cmdline: list[PathString] = [
            "env",
            "HOME=/", # Make sure rpm doesn't pick up ~/.rpmmacros and ~/.rpmrc.
            dnf,
            "--assumeyes",
            "--best",
            f"--releasever={context.config.release}",
            f"--installroot={context.root}",
            "--setopt=keepcache=1",
            "--setopt=logdir=/var/log",
            f"--setopt=cachedir=/var/cache/{cls.subdir(context.config)}",
            f"--setopt=persistdir=/var/lib/{cls.subdir(context.config)}",
            f"--setopt=install_weak_deps={int(context.config.with_recommends)}",
            "--setopt=check_config_file_age=0",
            "--disable-plugin=*" if dnf.endswith("dnf5") else "--disableplugin=*",
            "--enable-plugin=builddep" if dnf.endswith("dnf5") else "--enableplugin=builddep",
        ]

        if ARG_DEBUG.get():
            cmdline += ["--setopt=debuglevel=10"]

        if not context.config.repository_key_check:
            cmdline += ["--nogpgcheck"]

        if context.config.repositories:
            opt = "--enable-repo" if dnf.endswith("dnf5") else "--enablerepo"
            cmdline += [f"{opt}={repo}" for repo in context.config.repositories]

        if context.config.cacheonly == Cacheonly.always:
            cmdline += ["--cacheonly"]
        else:
            cmdline += ["--setopt=metadata_expire=never"]
            if dnf == "dnf5":
                cmdline += ["--setopt=cacheonly=metadata"]

        if not context.config.architecture.is_native():
            cmdline += [f"--forcearch={context.config.distribution.architecture(context.config.architecture)}"]

        if not context.config.with_docs:
            cmdline += ["--no-docs" if dnf.endswith("dnf5") else "--nodocs"]

        if dnf.endswith("dnf5"):
            cmdline += ["--use-host-config"]
        else:
            cmdline += [
                "--config=/etc/dnf/dnf.conf",
                "--setopt=reposdir=/etc/yum.repos.d",
                "--setopt=varsdir=/etc/dnf/vars",
            ]

        return cmdline

    @classmethod
    def invoke(
        cls,
        context: Context,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        apivfs: bool = False,
        stdout: _FILE = None,
    ) -> CompletedProcess:
        try:
            with finalize_source_mounts(
                context.config,
                ephemeral=os.getuid() == 0 and context.config.build_sources_ephemeral,
            ) as sources:
                return run(
                    cls.cmd(context) + [operation,*arguments],
                    sandbox=(
                        context.sandbox(
                            network=True,
                            options=[
                                "--bind", context.root, context.root,
                                *cls.mounts(context),
                                *sources,
                                "--chdir", "/work/src",
                            ],
                        ) + (apivfs_cmd(context.root) if apivfs else [])
                    ),
                    env=context.config.environment,
                    stdout=stdout,
                )
        finally:
            # dnf interprets the log directory relative to the install root so there's nothing we can do but to remove
            # the log files from the install root afterwards.
            if (context.root / "var/log").exists():
                for p in (context.root / "var/log").iterdir():
                    if any(p.name.startswith(prefix) for prefix in ("dnf", "hawkey", "yum")):
                        p.unlink()

    @classmethod
    def sync(cls, context: Context, options: Sequence[str] = ()) -> None:
        cls.invoke(
            context,
            "makecache",
            arguments=[
                "--refresh",
                *(["--setopt=cacheonly=none"] if cls.executable(context.config) == "dnf5" else []),
                *options,
            ],
        )

    @classmethod
    def createrepo(cls, context: Context) -> None:
        run(["createrepo_c", context.packages],
            sandbox=context.sandbox(options=["--bind", context.packages, context.packages]))

        (context.pkgmngr / "etc/yum.repos.d/mkosi-local.repo").write_text(
            textwrap.dedent(
                """\
                [mkosi]
                name=mkosi
                baseurl=file:///work/packages
                gpgcheck=0
                metadata_expire=never
                priority=50
                """
            )
        )

        cls.sync(context, options=["--disablerepo=*", "--enablerepo=mkosi"])
