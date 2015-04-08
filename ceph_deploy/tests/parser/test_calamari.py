import pytest

from ceph_deploy.cli import get_parser
from ceph_deploy.tests.util import assert_too_few_arguments


class TestParserCalamari(object):

    def setup(self):
        self.parser = get_parser()

    def test_calamari_help(self, capsys):
        with pytest.raises(SystemExit):
            self.parser.parse_args('calamari --help'.split())
        out, err = capsys.readouterr()
        assert 'usage: ceph-deploy calamari' in out
        assert 'positional arguments:' in out
        assert 'optional arguments:' in out

    def test_calamari_connect_help(self, capsys):
        with pytest.raises(SystemExit):
            self.parser.parse_args('calamari connect --help'.split())
        out, err = capsys.readouterr()
        assert 'usage: ceph-deploy calamari connect' in out
        assert 'positional arguments:' in out
        assert 'optional arguments:' in out

    def test_calamari_connect_host_required(self, capsys):
        with pytest.raises(SystemExit):
            self.parser.parse_args('calamari connect'.split())
        out, err = capsys.readouterr()
        assert_too_few_arguments(err)

    def test_calamari_connect_one_host(self):
        args = self.parser.parse_args('calamari connect --master master.ceph.com host1'.split())
        assert args.hosts == ['host1']

    def test_calamari_connect_multiple_hosts(self):
        hostnames = ['host1', 'host2', 'host3']
        args = self.parser.parse_args('calamari connect --master master.ceph.com'.split() + hostnames)
        assert args.hosts == hostnames

    def test_calamari_connect_master_argument_is_required(self, capsys):
        with pytest.raises(SystemExit):
            self.parser.parse_args('calamari connect host1'.split())
        out, err = capsys.readouterr()
        # This is a little sloppy; python 2.x prints "argument --master is required"
        # whereas python 3.x prints "the following arguments are required: --master"
        # so we test that there's some error output that includes usage information
        # and mentions "--master" and "required" :-/
        assert 'usage: ceph-deploy calamari' in err
        assert '--master' in err
        assert 'required' in err

    def test_calamari_connect_master_custom(self):
        args = self.parser.parse_args('calamari connect --master master.ceph.com host1'.split())
        assert args.master == "master.ceph.com"
