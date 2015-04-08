import errno
import logging
import os
from ceph_deploy import hosts, exc
from ceph_deploy.lib import remoto

#
# For use on SLES, `ceph-deploy calamari` doesn't use a calamari minion repo.
# Rather, it relies on your Ceph nodes already having access to a respository
# that containts salt-minion and its dependencies, as well as diamond, which
# salt-minion will in turn install.  This completely removes any need to
# specify repos, or alter your ~/.cephdeploy.conf file at all (which is what
# (http://calamari.readthedocs.org/en/latest/operations/minion_connect.html
# says you need to do).
#
# All you need to do to hook some set of Ceph nodes up to a calamari instance
# is run:
#
#   ceph-deploy calamari connect --master <calamari-fqdn> <node1> [<node2> ...]
#
# For example:
#
#   ceph-deploy calamari connect --master calamari.example.com \
#       ceph-0.example.com ceph-1.example.com ceph-2.example.com
#
# Or, if you are running ceph-deploy from your calamari host:
#
#   ceph-deploy calamari connect --master $(hostname -f) \
#       ceph-0.example.com ceph-1.example.com ceph-2.example.com
#

LOG = logging.getLogger(__name__)


def distro_is_supported(distro_name):
    """
    An enforcer of supported distros that can differ from what ceph-deploy
    supports.
    """
    supported = ['suse']
    if distro_name in supported:
        return True
    return False


def connect(args):
    for hostname in args.hosts:
        distro = hosts.get(hostname, username=args.username)
        if not distro_is_supported(distro.normalized_name):
            raise exc.UnsupportedPlatform(
                distro.name,
                distro.codename,
                distro.release
            )

        LOG.info(
            'Distro info: %s %s %s',
            distro.name,
            distro.release,
            distro.codename
        )
        LOG.info('assuming that a repository with Calamari packages is already configured.')
        LOG.info('Refer to the docs for examples (http://ceph.com/ceph-deploy/docs/conf.html)')

        rlogger = logging.getLogger(hostname)
        rlogger.info('installing calamari-minion package on %s' % hostname)

        # Emplace minion config prior to installation so that it is present
        # when the minion first starts.
        minion_config_dir = os.path.join('/etc/salt/', 'minion.d')
        minion_config_file = os.path.join(minion_config_dir, 'calamari.conf')

        rlogger.debug('creating config dir: %s' % minion_config_dir)
        distro.conn.remote_module.makedir(minion_config_dir, [errno.EEXIST])

        rlogger.debug(
            'creating the calamari salt config: %s' % minion_config_file
        )
        distro.conn.remote_module.write_file(
            minion_config_file,
            'master: %s\n' % args.master
        )

        distro.packager.install('salt-minion')

        remoto.process.run(
            distro.conn,
            ['systemctl', 'enable', 'salt-minion']
        )

        remoto.process.run(
            distro.conn,
            ['systemctl', 'start', 'salt-minion']
        )

        distro.conn.exit()


def calamari(args):
    if args.subcommand == 'connect':
        connect(args)


def make(parser):
    """
    Install and configure Calamari nodes. Assumes that a repository with
    Calamari packages is already configured. Refer to the docs for examples
    (http://ceph.com/ceph-deploy/docs/conf.html)
    """
    calamari_parser = parser.add_subparsers(dest='subcommand')

    calamari_connect = calamari_parser.add_parser(
        'connect',
        help='Configure host(s) to connect to Calamari master'
    )
    calamari_connect.add_argument(
        '--master',
        required=True,
        help="The fully qualified domain name of the Calamari server"
    )
    calamari_connect.add_argument(
        'hosts',
        nargs='+',
    )

    parser.set_defaults(
        func=calamari,
    )
