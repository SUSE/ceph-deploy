import argparse
import json
import logging
import os
import re
import sys
import time
from textwrap import dedent
import ConfigParser

from cStringIO import StringIO
from ceph_deploy import exc
from ceph_deploy import conf
from ceph_deploy.cliutil import priority
from ceph_deploy import hosts

from ceph_deploy.util import constants, system
from ceph_deploy.lib import remoto
from ceph_deploy.util.services import init_system, init_exception_service
import ceph_deploy.hosts.remotes as remotes
LOG = logging.getLogger(__name__)

## Templates

template_apache2_rgw_conf = """FastCgiExternalServer /srv/www/radosgw/{scriptName} -socket {socket}


<VirtualHost *:{port}>

        {noServerName}ServerName {fqdn}
        #ServerAdmin webmaster@example.com
        DocumentRoot /srv/www/radosgw
        RewriteEngine On
        RewriteRule  {redirect} /{scriptName}?%{{QUERY_STRING}} [E=HTTP_AUTHORIZATION:%{{HTTP:Authorization}},L]

        <IfModule mod_fastcgi.c>
        <Directory /srv/www/radosgw>
                        Options +ExecCGI
                        AllowOverride All
                        SetHandler fastcgi-script
                        Require all granted
                        AuthBasicAuthoritative Off
                </Directory>
        </IfModule>

        AllowEncodedSlashes On
        ErrorLog /var/log/apache2/rgw-{scriptName}-error.log
        CustomLog /var/log/apache2/rgw-{scriptName}-access.log combined
        ServerSignature Off

</VirtualHost>"""

template_radosgw_s3gw_fcgi = """#!/bin/sh
exec /usr/bin/radosgw -c /etc/ceph/ceph.conf -n {entity}
"""



apache_conf_d = "/etc/apache2/conf.d"
apache_fcgi_d = "/srv/www/radosgw"

# entity translation

def rgw_name2entity(entity):
    prefix = "client.radosgw."
    if entity.startswith(prefix):
        return entity
    return prefix + entity

def rgw_entity2name(entity):
    prefix = "client.radosgw."
    if entity.startswith(prefix):
        return entity[15:]
    return entity

#Direcotry filter

def dir_filter(conn, directory, pattern):
    output = set()
    prog = re.compile(pattern)
    filelist = conn.remote_module.listdir(directory)
    for filename in filelist:
        if not prog.search(filename):
            continue
        output.add(filename)
    return output

#Key manipulation

def rgw_key_gen(conn, keypath, name):
    # TODO : Remove keys with '/' in the name.
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph-authtool',
            '-C',
            '-n',
            name,
            '--gen-key',
            keypath
            ],
        )
    if rc > 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error('exit code from command was: %s' % rc)
        LOG.error('Could not generate key')
        return False
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph-authtool',
            '-n',
            name,
            '--cap',
            'osd',
            'allow rwx',
            '--cap',
            'mon',
            'allow rwx',
            keypath
            ],
        )
    if rc > 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error('exit code from command was: %s' % rc)
        LOG.error('Could not set capabilities')
        return False
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph',
            'auth',
            'add',
            name,
            '--in-file',
            keypath
            ],
        )
    if rc > 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error('exit code from command was: %s' % rc)
        LOG.error('Could not add auth info for %s' % name)
        return False
    return True

def rgw_key_list(conn):
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph',
            '-f',
            'json',
            'auth',
            'list'
            ],
        )
    if rc != 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error("rc=%s" % (rc))
        return None
    auth = json.loads("".join(stdout).strip())
    output = {}
    for item in auth['auth_dump']:
        output[item['entity']] = item
    return output


def rgw_key_deauth(conn,entity):
    remoto.process.run(
        conn,
        [
            'ceph',
            'auth',
            'del',
            entity
            ],
        )

def rgw_key_dict2string(key_as_dict):
    key = key_as_dict['key']
    caps = key_as_dict['caps']
    entity = key_as_dict['entity']
    output = """[{entity}]\n\tkey = {key}\n""".format(
        entity = key_as_dict['entity'],
        key = key_as_dict['key'])
    for auth in key_as_dict['caps']:
        output += '\tcaps {auth} = "{privalages}"\n'.format(
                auth = auth,
                privalages = key_as_dict['caps'].get(auth)
                )
    return output




#cfg manipulation

def rgw_cfg_save(args,cfg):

    # now save changes
    # TODO merge with code from ceph_deploy.new.new
    path = '{name}.conf'.format(
        name=args.cluster,
        )
    LOG.debug('Writing initial config to %s...', path)
    tmp = '%s.tmp' % path
    with file(tmp, 'w') as f:
        cfg.write(f)
    try:
        os.rename(tmp, path)
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise exc.ClusterExistsError(path)
        else:
            raise


def rgw_cfg_get(cfg):
    output = {}
    for rgw_section in cfg.sections():
        if rgw_section[:15] != "client.radosgw.":
            continue
        sect_details = {}
        sect_details['host'] = cfg.safe_get(rgw_section, 'host')
        sect_details['keyring'] = cfg.safe_get(rgw_section, 'keyring')
        sect_details['rgw socket path'] = cfg.safe_get(rgw_section, 'rgw socket path')
        sect_details['log file'] = cfg.safe_get(rgw_section, 'log file')
        output[rgw_section] = sect_details
    return output

def rgw_cfg_mon_hosts(cfg):
    raw = cfg.safe_get('global', 'mon_initial_members')
    monhosts = set()
    for host in raw.split(','):
        monhosts.add(host.strip())
    return monhosts

def rgw_cfg_push_hosts(cfg):
    pushhosts = rgw_cfg_mon_hosts(cfg)
    rgw_cfg = rgw_cfg_get(cfg)
    for section in rgw_cfg:
        host_name = rgw_cfg[section].get('host',None)
        if host_name == None:
            continue
        pushhosts.add(host_name)
    return pushhosts





def config_push(args,host_list):
    # similar to ceph_deploy.config.config_push but takes hostnames as paramter.
    conf_data = conf.ceph.load_raw(args)
    errors = 0
    for hostname in host_list:
        LOG.debug('Pushing config to %s', hostname)
        try:
            distro = hosts.get(hostname, username=args.username)
            distro.conn.remote_module.write_conf(
                args.cluster,
                conf_data,
                args.overwrite_conf,
            )
            distro.conn.exit()
        except RuntimeError as e:
            LOG.error(e)
            errors += 1

    if errors:
        raise exc.GenericError('Failed to config %d hosts' % errors)


# pool tools

def pool_list(conn):
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph',
            '-f',
            'json',
            'osd',
            'lspools'
            ],
        )
    if rc != 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error("rc=%s" % (rc))
        return None
    auth = json.loads("".join(stdout).strip())
    return auth

def pool_add(conn, name, number):
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph',
            'osd',
            'pool',
            'create',
            name,
            str(number)
            ],
        )
    if rc != 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error("rc=%s" % (rc))
        return False
    return True

def pool_del(conn, name):
    stdout, stderr, rc = remoto.process.check(
        conn,
        [
            'ceph',
            'osd',
            'pool',
            'delete',
            name,
            name,
            '--yes-i-really-really-mean-it'
            ],
        )
    if rc != 0:
        for line in stderr:
            conn.logger.error(line)
        for line in stdout:
            conn.logger.error(line)
        conn.logger.error("rc=%s" % (rc))
        return False
    return True


# helper functions

def rgw_pools_create(conn):
    requiredPools = set([".rgw",
            ".rgw.control",
            ".rgw.gc",
            ".log",
            ".intent-log",
            ".usage",
            ".users",
            ".users.email",
            ".users.swift",
            ".users.uid"
        ])
    allpools = pool_list(conn)
    if allpools == None:
        LOG.error("Failed to list available pools")
        return False
    foundnames = set()
    foundnumbers = set()
    for pool in allpools:
        name = pool[u'poolname']
        number = pool[u'poolnum']
        foundnames.add(name)
        foundnumbers.add(number)
    counter = 0
    rc = True
    for name in requiredPools.difference(foundnames):
        while counter in foundnumbers:
            counter = counter + 1
        foundnumbers.add(counter)
        tmp_rc = pool_add(conn, name, counter)
        if not tmp_rc:
            LOG.error("Failed to add pool:%s" % (name))
            rc = False
    return True


# helper functions : apache

def apache_setup_modules(distro):
    apache_sysconfig_path = "/etc/sysconfig/apache2"
    apache_sysconfig_key = "APACHE_MODULES"
    modules_required = set(["actions",
        "alias",
        "auth_basic",
        "authn_file",
        "authz_host",
        "authz_groupfile",
        "authz_user",
        "autoindex",
        "cgi",
        "dir",
        "env",
        "expires",
        "include",
        "log_config",
        "mime",
        "negotiation",
        "setenvif",
        "ssl",
        "socache_shmcb",
        "userdir",
        "reqtimeout",
        "authn_core",
        "authz_core",
        "fastcgi",
        "rewrite"])
    if not distro.conn.remote_module.path_exists(apache_sysconfig_path):
        LOG.error("failed to find '%s'" % (apache_sysconfig_path))
        raise SystemExit("not found '%s'" % (apache_sysconfig_path))
    current_value = distro.conn.remote_module.sysconfig_read(apache_sysconfig_path,
        apache_sysconfig_key)
    if current_value == None:
        LOG.error("failed to read '%s' and get '%s' value" % (apache_sysconfig_path,
            apache_sysconfig_key))
        raise SystemExit('Failed to setup apache modules')
    current_modules = set()
    for module in current_value.split(" "):
        module_stripped = module.strip()
        if len(module_stripped) == 0:
            continue
        current_modules.add(module_stripped)
    apache_sysconfig_value_new = str(current_value)
    for item in modules_required.difference(current_modules):
        apache_sysconfig_value_new += " %s" % (item)
    apache_sysconfig_value_new = apache_sysconfig_value_new.strip()
    LOG.info("Setting apache modules to :%s" % (apache_sysconfig_value_new))
    distro.conn.remote_module.sysconfig_write(apache_sysconfig_path,
        apache_sysconfig_key,
        apache_sysconfig_value_new)


def apache_setup(distro, **kwargs):
    apache_setup_modules(distro)

    fqdn = kwargs.get('fqdn', None)
    port = kwargs.get('port', None)
    entity = kwargs.get('entity', None)
    redirect = kwargs.get('redirect', None)
    socket = kwargs.get('socket', None)
    ServerNameCommented = "#"
    # validate
    if entity == None:
        LOG.error("No entity provided")
        raise SystemExit('No entity provided')
    if socket == None:
        LOG.error("No socket provided")
        raise SystemExit('No socket provided')
    if port == None:
        port = 80
        LOG.error("defaulting port to:%s" % (port))
    if redirect == None:
        redirect = "^/(.*)"
        LOG.error("defaulting redirect to:%s" % (redirect))



    # Now we gen the files
    atributes = []
    if entity != None:
        atributes.append(entity)
    if port != None:
        atributes.append(str(port))
    attribString = "_testing"
    if len(atributes) > 0:
        attribString = str("_%s" % ("_".join(atributes)))


    scriptName = "s3gw_%s.fcgi" % (port)
    conf_name = "s3gw_%s.conf" % (port)

    content = template_apache2_rgw_conf.format(port = port,
        noServerName = ServerNameCommented,
        fqdn = fqdn,
        scriptName = scriptName,
        redirect = redirect,
        socket = socket)
    path_conf = "%s/%s" % (apache_conf_d, conf_name)
    if distro.conn.remote_module.path_exists(path_conf):
        LOG.info("File exists Skipping:%s" % (path_conf))
    else:
        LOG.info("Writing:%s" % (path_conf))
        distro.conn.remote_module.write_file(path_conf, content)
    content = template_radosgw_s3gw_fcgi.format(entity = entity)
    scriptpath = "%s/%s" % (apache_fcgi_d,scriptName)
    if distro.conn.remote_module.path_exists(scriptpath):
        LOG.info("File exists Skipping:%s" % (scriptpath))
    else:
        LOG.info("Writing:%s" % (scriptpath))
        distro.conn.remote_module.write_file(scriptpath, content)
    distro.conn.remote_module.chmod(scriptpath, 0755)
    #distro.conn.remote_module.chown(scriptpath, "wwwrun","www")





def apache_teardown_conf_d(distro):
    LOG.info("apache_conf_d=%s" % (apache_conf_d))
    #LOG.info("filelist=%s" % (str(", ".join(filelist))))
    return set(map( lambda m: apache_conf_d + "/" + m, dir_filter(distro.conn,apache_conf_d,"^ceph_radosgw_.*conf")))


def apache_teardown_fcgi_d(distro):
    LOG.info("apache_fcgi_d=%s" % (apache_fcgi_d))
    #LOG.info("filelist=%s" % (str(", ".join(filelist))))
    return set(map( lambda m: apache_fcgi_d + "/" + m, dir_filter(distro.conn,apache_fcgi_d,"^s3gw_.*fcgi")))


def processfilenames(conn, directory,filelist,search):
    output = set()
    for filepath in filelist:
        exists = conn.remote_module.path_exists(filepath)
        if exists == False:
            LOG.error("no file=%s" % (filepath))
            continue

        content = conn.remote_module.get_file(filepath)
        if content == None:
            LOG.debug("no content=%s" % (filename))
            continue
        for line in content.split('\n'):
            found_rc = line.find(search)
            if found_rc < 0:
                continue
            output.add(filepath)
            break
    return output

def apache_info(distro, **kwargs):


    entity = kwargs.get('entity', None)
    cfg = kwargs.get('cfg', None)

    if entity == None:
        LOG.error("No entity provided")
        raise SystemExit('No entity provided')
    if cfg == None:
        LOG.error("No cfg provided")
        raise SystemExit('No cfg provided')

    rgw_cfg = rgw_cfg_get(cfg)

    unfiltered_conf = apache_teardown_conf_d(distro)
    unfiltered_fcgi = apache_teardown_fcgi_d(distro)


    output = { "entity" : {},
            "missing" : {}
        }
    found_fcgi = set()
    found_conf = set()


    for entity in rgw_cfg.keys():
        LOG.debug("entity=%s" % (entity))
        entity_content = {}
        sock_path = str(rgw_cfg[entity]['rgw socket path'])
        filtered_conf = processfilenames(distro.conn,apache_conf_d,unfiltered_conf,str(sock_path))
        found_conf = found_conf.union(filtered_conf)
        filtered_fcgi = processfilenames(distro.conn,apache_fcgi_d,unfiltered_fcgi,str(entity))
        found_fcgi = found_fcgi.union(filtered_fcgi)
        entity_content["conf"] = filtered_conf
        entity_content["fcgi"] = filtered_fcgi
        output["entity"][entity] = entity_content



    missing_conf = unfiltered_conf.difference(found_conf)
    missing_fcgi = unfiltered_fcgi.difference(found_fcgi)
    for filename in missing_conf.union(missing_fcgi):
        LOG.warning("Unknown file:%s" % (filename))
    output["missing"]["conf"] = missing_conf
    output["missing"]["fcgi"] = missing_fcgi
    return output


def apache_info_all(distro,**kwargs):
    output = {}
    entity = kwargs.get('entity', None)
    if entity == None:
        LOG.error("No entity provided")
        raise SystemExit('No entity provided')
    installed = kwargs.get('installed', None)
    if installed == None:
        LOG.error("No installed provided")
        raise SystemExit('No installed provided')
    username = kwargs.get('username', None)

    for hostname in installed:
        try:
            distro = hosts.get(hostname, username=username)
        except:
            LOG.error("failed to connect to '%s'" % (hostname))
            raise SystemExit('Failed to setup radosgw')
        cfg = apache_info(distro, **kwargs)
        output[hostname] = cfg
    return output




def apache_teardown(distro, **kwargs):
    entity = kwargs.get('entity', None)
    if entity == None:
        LOG.error("No entity provided")
        raise SystemExit('No entity provided')
    cfg = apache_info(distro, **kwargs)
    todelete = cfg['entity'][entity]["conf"].union(cfg['entity'][entity]["fcgi"])
    for filename in todelete:
        LOG.info("deleting file:%s" % (filename))
        distro.conn.remote_module.unlink(filename)

# Top level functions


def rgw_list(args, cfg):
    for rgw_section in cfg.sections():
        if rgw_section[:15] != "client.radosgw.":
            continue
        host = cfg.safe_get(rgw_section, 'host')
        print "%s:%s" % (host, rgw_entity2name(rgw_section))


def rgw_prepare(args, cfg):
    monhosts = rgw_cfg_mon_hosts(cfg)
    config = rgw_cfg_get(cfg)
    prepare_wanted = set()
    wantedhosts = set(monhosts)
    push_hosts = rgw_cfg_push_hosts(cfg)
    map_entity2host = {}
    map_entity2fqdn = {}
    map_entity2port = {}
    map_entity2redirect = {}
    for (hostname, instance_name, fqdn, port, redirect) in args.rgw:
        if instance_name == None:
            instance_name = hostname
            LOG.info("%s:Defaulting instance to:%s" % (hostname, hostname))
        instance = rgw_name2entity(instance_name)
        instance_name = rgw_entity2name(instance)
        prepare_wanted.add(instance)
        push_hosts.add(hostname)
        map_entity2host[instance] = hostname
        if fqdn == None:
            port = 80
            LOG.info("%s:Not setting virtual host fqdn" % (instance_name))

        map_entity2fqdn[instance] = fqdn
        if port == None:
            port = 80
            LOG.info("%s:Defaulting port to:%s" % (instance_name, port))
        try:
            portNum = int(port)
        except TypeError:
            portNum = 80
            LOG.warning("%s:Defaulting port to:%s" % (instance_name, portNum))
        map_entity2port[instance] = portNum
        if (redirect == None) or (len(redirect) == 0):
            redirect = "^/(.*)"
            LOG.info("%s:Defaulting redirect to:%s" % (instance_name, redirect))
        map_entity2redirect[instance] = redirect
    # now we use a mon to remove auth enities
    pools_setup = False
    for host_mon in monhosts:
        try:
            distro = hosts.get(host_mon, username=args.username)
        except:
            # try next host
            continue
        if rgw_pools_create(distro.conn):
            pools_setup = True
            # because we dont want to do this on each mon node
            break
        LOG.info("Failed to setup pools onhost:%s" % (host_mon))
    if not pools_setup:
        raise SystemExit('Failed to setup pools')
    # we now know which hosts are present and missing.
    present_cfg = prepare_wanted.intersection(config.keys())
    missing_cfg = prepare_wanted.difference(config.keys())
    failedhosts = set()
    for entity in prepare_wanted:
        hostname = map_entity2host.get(entity)
        wantedhosts.add(hostname)
    installed = set()
    for entity in prepare_wanted.difference(failedhosts):
        hostname = map_entity2host[entity]
        try:
            distro = hosts.get(hostname, username=args.username)
        except:
            LOG.error("failed to connect to '%s'" % (hostname))
            raise SystemExit('Failed to setup radosgw')
        distro.pkg_refresh(distro)
        distro.pkg_install(
                distro,
                "ceph-radosgw"
            )
        installed.add(hostname)

    # now we build cfg we will apply.
    model = apache_info_all(distro,
            username = args.username,
            installed = installed,
            entity = entity,
            cfg = cfg)

    cfg_changed = False
    # now add keys
    for entity in prepare_wanted:
        hostname = map_entity2host[entity]
        if not hostname in installed:
            LOG.warning("Skipping keys for '%s' as cant reach %s" % (entity,hostname))
            continue
        distro = hosts.get(hostname, username=args.username)
        keyring = "/etc/ceph/ceph.{instance}.keyring".format(
                instance=entity,
            )
        rgw_socket_path = "/var/run/ceph-radosgw/ceph.{instance}.fastcgi.sock".format(
                instance=entity,
            )
        log_file = "/var/log/ceph-radosgw/ceph.{instance}.log".format(
                instance=entity,
            )
        admin_socket = "/var/run/ceph-radosgw/ceph.{instance}.asok".format(
                instance=entity,
            )
        if not cfg.has_section(entity):
            cfg.add_section(entity)
            cfg_changed = True
        hostname_tmp = hostname
        try:
            hostname_tmp = cfg.get(entity,'host')
        except (ConfigParser.NoOptionError):
            cfg.set(entity, 'host', hostname)
            cfg_changed = True
        if hostname_tmp != hostname:
            LOG.error("'%s' is already installed on '%s' and not '%s'" % (
                entity,
                hostname_tmp,
                hostname
                ))
            raise SystemExit('Failed to setup radosgw')
        try:
            keyring = cfg.get(entity,'keyring')
        except (ConfigParser.NoOptionError):
            cfg.set(entity, 'keyring', keyring)
            cfg_changed = True
        try:
            rgw_socket_path = cfg.get(entity,'rgw socket path')

        except (ConfigParser.NoOptionError):
            cfg.set(entity, 'rgw socket path', rgw_socket_path)
            cfg_changed = True
        try:
            log_file = cfg.get(entity,'log file')
        except (ConfigParser.NoOptionError):
            cfg_changed = True
            cfg.set(entity, 'log file', log_file)
        try:
            admin_socket = cfg.get(entity,'admin socket')
        except (ConfigParser.NoOptionError):
            cfg_changed = True
            cfg.set(entity, 'admin socket', admin_socket)
        if not cfg.has_section(entity):
            cfg.add_section(entity)
            cfg_changed = True
        cfg_changed = True


    # Now we need to make the keys.
    auth = None
    missing_auth = set()
    for host_mon in monhosts:
        # We need to create keys on a mon node.
        try:
            distro = hosts.get(host_mon, username=args.username)
        except:
            # try next host
            continue
        auth = rgw_key_list(distro.conn)
        if auth == None:
            LOG.warning("Could not list the keys on host:%s" % (host_mon))
            continue
        missing_auth = prepare_wanted.difference(auth.keys())
        for entity in missing_auth:
            keypath = cfg.get(entity,'keyring')
            if not rgw_key_gen(distro.conn, keypath, entity):
                LOG.warning("Could not add key using host '%s' for entitiy '%s'" % (host_mon,entity))
                continue
        # we only need to do this on one host
        auth = rgw_key_list(distro.conn)
        if auth == None:
            LOG.warning("Could not list the keys on host:%s" % (host_mon))
            continue
        missing_auth = prepare_wanted.difference(auth.keys())
        if len(missing_auth) > 0:
            continue
        break
    if auth == None:
        raise SystemExit('Failed to get auth details')

    present_auth = prepare_wanted.intersection(auth.keys())
    missing_auth = prepare_wanted.difference(auth.keys())
    if len(missing_auth) > 0:
        for missing in missing_auth:
            LOG.error("Failed to add key for :%s" % (missing))
        raise SystemExit('Failed to get keys')

    for entity in present_auth:
        keypath = cfg.get(entity,'keyring')
        rgw_socket_path = cfg.get(entity,'rgw socket path')
        localkeypath = os.path.basename(keypath)
        string2write = rgw_key_dict2string(auth[entity])
        if not os.path.exists(localkeypath):
            LOG.info("creating file:'%s'" % (localkeypath))
            remotes.write_file(localkeypath,string2write)
        hostname = map_entity2host[entity]
        try:
            distro = hosts.get(hostname, username=args.username)
        except:
            # try next host
            continue


        distro.conn.remote_module.write_keyring(keypath,string2write)
        distro.conn.remote_module.chmod(keypath, 0640)
        distro.conn.remote_module.chown(keypath, "root","www")
        apache_setup(distro,
            entity = entity,
            socket = rgw_socket_path,
            fqdn = map_entity2fqdn[entity],
            port = map_entity2port[entity],
            redirect = map_entity2redirect[entity])
    if cfg_changed:
        rgw_cfg_save(args,cfg)
        config_push(args,push_hosts)

    if len(failedhosts) > 0:
        for item in failedhosts:
            LOG.error("failed to connect to '%s'" % (item))
        raise SystemExit('Failed to setup radosgw')
    if len(failedhosts) > 0:
        for item in failedhosts:
            LOG.error("failed to install ceph-radosgw on '%s'" % (item))
        raise SystemExit('Failed to setup radosgw')
    if len(present_cfg) > 0:
        for item in present_cfg:
            LOG.warning("The following rgw config already exit '%s'" % (item))
        raise SystemExit('Failed to setup radosgw')

def rgw_activate(args, cfg):
    config = rgw_cfg_get(cfg)
    map_entity2host = {}
    prepare_wanted = set()
    for (hostname, instance_name, fqdn, port, redirect) in args.rgw:
        if instance_name == None:
            instance_name = hostname
            LOG.info("%s:Defaulting instance to:%s" % (hostname, hostname))
        instance = rgw_name2entity(instance_name)
        prepare_wanted.add(instance)
        map_entity2host[instance] = hostname
    instance_activate_wanted = set()
    present_cfg = prepare_wanted.intersection(config.keys())
    missing_cfg = prepare_wanted.difference(config.keys())
    failedhosts = set()
    for entity in present_cfg:
        host_name = map_entity2host[entity]

        try:
            distro = hosts.get(host_name, username=args.username)
        except:
            # try next host
            continue
        init = init_system(connection = distro.conn,
            init_type = distro.choose_init(),
            service_name_mapping = distro.service_mapping)
        #init.init_type = distro.choose_init()
        entity_name = rgw_entity2name(entity)
        try:
            init.start("apache")
        except init_exception_service:
            LOG.error("Failed starting apache")
        try:
            init.enable("apache")
        except init_exception_service:
            LOG.error("Failed enabling apache")
        try:
            init.start("ceph-radosgw",[entity_name])
        except init_exception_service:
            LOG.error("Failed starting ceph-radosgw %s" % (entity_name))
        init.status("ceph-radosgw",[entity_name])
        init.enable("ceph-radosgw",[entity_name])


    if len(failedhosts) > 0:
        for item in failedhosts:
            LOG.error("failed to connect to '%s'" % (item))
        raise SystemExit('Failed to setup radosgw')


def rgw_create(args, cfg):
    rgw_prepare(args, cfg)
    rgw_activate(args, cfg)

def rgw_delete(args, cfg):
    monhosts = rgw_cfg_mon_hosts(cfg)
    push_hosts = rgw_cfg_push_hosts(cfg)
    config = rgw_cfg_get(cfg)
    delete_wanted = set()
    for (hostname, instance_name, fqdn, port, redirect) in args.rgw:
        if instance_name == None:
            instance_name = hostname
            LOG.info("%s:Defaulting instance to:%s" % (hostname, hostname))
        instance = rgw_name2entity(instance_name)
        delete_wanted.add(instance)
        push_hosts.add(hostname)
    # we now know which hosts are present and missing.
    present = delete_wanted.intersection(config.keys())
    missing = delete_wanted.difference(config.keys())

    # rather than just deauth all entities only remove
    # them if we can found them in the config file.
    entities_deauth = set()
    for entity in present:
        hostname = config[entity].get("host",None)
        if hostname == None:
            continue
        push_hosts.add(hostname)
        # now we process the details.
        distro = hosts.get(hostname, username=args.username)
        apache_teardown(distro,
            entity = entity,
            cfg = cfg
        )
        keyring = config[entity]["keyring"]
        keyring_loacal_name = os.path.basename(keyring)
        if os.path.isfile(keyring_loacal_name):
            LOG.debug("delete local file '%s'" % (keyring_loacal_name))
            os.remove(keyring_loacal_name)

        distro = hosts.get(hostname, username=args.username)
        init = init_system(connection = distro.conn,
            init_type = distro.choose_init(),
            service_name_mapping = distro.service_mapping)
        entity_name = rgw_entity2name(entity)
        try:
            init.stop("ceph-radosgw",[entity_name])
        except init_exception_service:
            LOG.error("Failed stopping ceph-radosgw %s" % (entity_name))
        try:
            init.disable("ceph-radosgw",[entity_name])
        except init_exception_service:
            LOG.error("Failed disabling ceph-radosgw %s" % (entity_name))
        try:
            init.stop("apache")
        except init_exception_service:
            LOG.error("Failed stopping apache")
        try:
            init.disable("apache")
        except init_exception_service:
            LOG.error("Failed disabling apache")
        keypath = cfg.get(entity,'keyring')
        if distro.conn.remote_module.path_exists(keyring):
            distro.conn.remote_module.unlink(keyring)
            LOG.info("deleted %s" % (keyring))
        entities_deauth.add(entity)
    # now we use a mon to remove auth enities
    auth = None
    for host_mon in monhosts:
        try:
            distro = hosts.get(host_mon, username=args.username)
        except:
            # try next host
            continue
        auth = rgw_key_list(distro.conn)
        if auth == None:
            LOG.warning("Could not establish the auuth on host:%s" % (host_mon))
            continue
        for entity in entities_deauth.intersection(auth.keys()):
            rgw_key_deauth(distro.conn,entity)
        # because we dont want to do this on each mon node
        break
    if len(present) > 0:
        for entity in present:
            cfg.remove_section(entity)
        rgw_cfg_save(args,cfg)
        config_push(args,push_hosts)

    if len(missing) > 0:
        for item in missing:
            LOG.error("The following rgw does not exist '%s'" % (item))
        raise SystemExit('Tired to delete non existent radosgw')
# cmd line

def rgw(args):
    cfg = conf.ceph.load(args)

    if args.subcommand == 'list':
        rgw_list(args, cfg)
    elif args.subcommand == 'prepare':
        rgw_prepare(args, cfg)
    elif args.subcommand == 'create':
        rgw_create(args, cfg)
    elif args.subcommand == 'activate':
        rgw_activate(args, cfg)
    elif args.subcommand == 'delete':
        rgw_delete(args, cfg)
    else:
        LOG.error('subcommand %s not implemented', args.subcommand)
        sys.exit(1)



def colon_separated(s):
    instance = None
    fqdn = None
    port = None
    redirect = None
    split_input = s.split(':')
    split_input_len = len(split_input)
    host = split_input[0]
    if len(host) == 0:
        raise argparse.ArgumentTypeError('invalid "host" host[:instance][:fqdn][:port][:redirect]')
    if split_input_len > 1:
        if len(split_input[1]) > 0:
            instance = split_input[1]
    if split_input_len > 2:
        if len(split_input[2]) > 0:
            fqdn = split_input[2]
    if split_input_len > 3:
        if len(split_input[3]) > 0:
            port = split_input[3]
    if split_input_len > 4:
        if len(split_input[4]) > 0:
            redirect = split_input[4]
    if split_input_len > 5:
        raise argparse.ArgumentTypeError('must be in form host[:instance][:fqdn][:port][:redirect]')
    return (host, instance, fqdn, port, redirect)



@priority(50)
def make(parser):
    """
    Prepare a rados gateway on remote host.
    """
    sub_command_help = dedent("""
    Manage Redos gateways on remote host.

    For paths, first prepare and then activate:

        ceph-deploy rgw prepare {rgw-host}[:{rgw-instance}][:fqdn][:port][:redirect]
        ceph-deploy rgw activate {rgw-host}[:{rgw-instance}][:fqdn][:port][:redirect]

    For the `create` command will do prepare and activate for you.

    parameters:
        rgw-host     : Host to install the rgw
        rgw-instance : Ceph instance name [rgw-host]
        fqdn         : Virtual host to listen [None]
        port         : Port to listen [80]
        redirect     : url redirect [^/(.*)]

    host names can also be virtual hosts if dns is set correctly.

    """
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = sub_command_help

    parser.add_argument(
        'subcommand',
        metavar='SUBCOMMAND',
        choices=[
            'list',
            'create',
            'prepare',
            'activate',
            'delete',
            ],
        help='list, create (prepare+activate), prepare, activate, or delete',
        )
    parser.add_argument(
        'rgw',
        nargs='*',
        metavar='HOST:NAME',
        type=colon_separated,
        help='host and name of radosgw',
        )

    parser.set_defaults(
        func=rgw,
        )

