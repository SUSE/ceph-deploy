from ceph_deploy.lib import remoto


def apt(conn, packages, *a, **kw):
    if isinstance(packages, str):
        packages = [packages]
    cmd = [
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'apt-get',
        'install',
        '--assume-yes',
    ]
    cmd.extend(packages)
    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def apt_remove(conn, packages, *a, **kw):
    if isinstance(packages, str):
        packages = [packages]

    purge = kw.pop('purge', False)
    cmd = [
        'apt-get',
        '-q',
        'remove',
        '-f',
        '-y',
        '--force-yes',
    ]
    if purge:
        cmd.append('--purge')
    cmd.extend(packages)

    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def apt_update(conn):
    cmd = [
        'apt-get',
        '-q',
        'update',
    ]
    return remoto.process.run(
        conn,
        cmd,
    )


def yum(conn, packages, *a, **kw):
    if isinstance(packages, str):
        packages = [packages]

    cmd = [
        'yum',
        '-y',
        'install',
    ]
    cmd.extend(packages)
    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def yum_remove(conn, packages, *a, **kw):
    cmd = [
        'yum',
        '-y',
        '-q',
        'remove',
    ]
    if isinstance(packages, str):
        cmd.append(packages)
    else:
        cmd.extend(packages)
    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def yum_clean(conn, item=None):
    item = item or 'all'
    cmd = [
        'yum',
        'clean',
        item,
    ]

    return remoto.process.run(
        conn,
        cmd,
    )


def rpm(conn, rpm_args=None, *a, **kw):
    """
    A minimal front end for ``rpm`. Extra flags can be passed in via
    ``rpm_args`` as an iterable.
    """
    rpm_args = rpm_args or []
    cmd = [
        'rpm',
        '-Uvh',
    ]
    cmd.extend(rpm_args)
    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def zypper(conn, packages, *a, **kw):
    if isinstance(packages, str):
        packages = [packages]

    cmd = [
        'zypper',
        '--non-interactive',
        'install',
    ]

    cmd.extend(packages)
    return remoto.process.run(
        conn,
        cmd,
        *a,
        **kw
    )


def zypper_remove(conn, packages, *a, **kw):

    executable = [
        'zypper',
        '--non-interactive',
        '--quiet'
    ]

    if isinstance(packages, str):
        packages = [packages]

    extra_flags = kw.pop('extra_remove_flags', None)
    cmd = executable + ['--ignore-unknown', 'remove']
    if extra_flags:
        if isinstance(extra_flags, str):
            extra_flags = [extra_flags]
        cmd.extend(extra_flags)
    cmd.extend(packages)
    stdout, stderr, exitrc = remoto.process.check(
            conn,
            cmd,
            **kw
        )
    # exitrc is 104 when package(s) not installed.
    if not exitrc in [0, 104]:
        raise RuntimeError("Failed to execute command: %s" % " ".join(cmd))
    return


def zypper_refresh(conn):
    cmd = [
        'zypper',
        '--non-interactive',
        'refresh',
        ]

    return remoto.process.run(
        conn,
        cmd
    )
