# -*- coding: utf-8 -*-

import json
import logging
import os
import shutil
import tempfile
import threading
import traceback
import xml.etree.ElementTree as ET
import zipfile

from functools import wraps
from contextlib import closing

import psycopg2

import odoo
from odoo import SUPERUSER_ID
from odoo.exceptions import UserError
import odoo.release
import odoo.sql_db
import odoo.tools

_logger = logging.getLogger(__name__)

class DatabaseExists(Warning):
    pass

#----------------------------------------------------------
# Master password required
#----------------------------------------------------------

def check_super(passwd):
    if passwd and odoo.tools.config.verify_admin_password(passwd):
        return True
    raise odoo.exceptions.AccessDenied()

# This should be moved to odoo.modules.db, along side initialize().
def _initialize_db(id, db_name, demo, lang, user_password, login='admin', country_code=None):
    try:
        db = odoo.sql_db.db_connect(db_name)
        with closing(db.cursor()) as cr:
            # TODO this should be removed as it is done by Registry.new().
            odoo.modules.db.initialize(cr)
            odoo.tools.config['load_language'] = lang
            cr.commit()

        registry = odoo.modules.registry.Registry.new(db_name, demo, None, update_module=True)

        with closing(db.cursor()) as cr:
            env = odoo.api.Environment(cr, SUPERUSER_ID, {})

            if lang:
                modules = env['ir.module.module'].search([('state', '=', 'installed')])
                modules.update_translations(lang)

            if country_code:
                countries = env['res.country'].search([('code', 'ilike', country_code)])
                if countries:
                    #env['res.company'].browse(1).country_id = countries[0]
                    env.ref('base.main_company').country_id= countries[0]

            # update admin's password and lang and login
            values = {'password': user_password, 'lang': lang}
            if login:
                values['login'] = login
                emails = odoo.tools.email_split(login)
                if emails:
                    values['email'] = emails[0]
            env.user.write(values)

            cr.execute('SELECT login, password FROM res_users ORDER BY login')
            cr.commit()
    except Exception, e:
        _logger.exception('CREATE DATABASE failed:')

def _create_empty_database(name):
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        chosen_template = odoo.tools.config['db_template']
        cr.execute("SELECT datname FROM pg_database WHERE datname = %s",
                   (name,))
        if cr.fetchall():
            raise DatabaseExists("database %r already exists!" % (name,))
        else:
            cr.autocommit(True)     # avoid transaction block
            cr.execute("""CREATE DATABASE "%s" ENCODING 'unicode' TEMPLATE "%s" """ % (name, chosen_template))

def exp_create_database(db_name, demo, lang, user_password='admin', login='admin', country_code=None):
    """ Similar to exp_create but blocking."""
    _logger.info('Create database `%s`.', db_name)
    _create_empty_database(db_name)
    _initialize_db(id, db_name, demo, lang, user_password, login, country_code)
    return True

def exp_duplicate_database(db_original_name, db_name):
    _logger.info('Duplicate database `%s` to `%s`.', db_original_name, db_name)
    odoo.sql_db.close_db(db_original_name)
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.autocommit(True)     # avoid transaction block
        _drop_conn(cr, db_original_name)
        cr.execute("""CREATE DATABASE "%s" ENCODING 'unicode' TEMPLATE "%s" """ % (db_name, db_original_name))

    registry = odoo.modules.registry.Registry.new(db_name)
    with registry.cursor() as cr:
        # if it's a copy of a database, force generation of a new dbuuid
        env = odoo.api.Environment(cr, SUPERUSER_ID, {})
        env['ir.config_parameter'].init(force=True)

    from_fs = odoo.tools.config.filestore(db_original_name)
    to_fs = odoo.tools.config.filestore(db_name)
    if os.path.exists(from_fs) and not os.path.exists(to_fs):
        shutil.copytree(from_fs, to_fs)
    return True

def _drop_conn(cr, db_name):
    # Try to terminate all other connections that might prevent
    # dropping the database
    try:
        # PostgreSQL 9.2 renamed pg_stat_activity.procpid to pid:
        # http://www.postgresql.org/docs/9.2/static/release-9-2.html#AEN110389
        pid_col = 'pid' if cr._cnx.server_version >= 90200 else 'procpid'

        cr.execute("""SELECT pg_terminate_backend(%(pid_col)s)
                      FROM pg_stat_activity
                      WHERE datname = %%s AND
                            %(pid_col)s != pg_backend_pid()""" % {'pid_col': pid_col},
                   (db_name,))
    except Exception:
        pass

def exp_drop(db_name):
    if db_name not in list_dbs(True):
        return False
    odoo.modules.registry.Registry.delete(db_name)
    odoo.sql_db.close_db(db_name)

    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.autocommit(True) # avoid transaction block
        _drop_conn(cr, db_name)

        try:
            cr.execute('DROP DATABASE "%s"' % db_name)
        except Exception, e:
            _logger.info('DROP DB: %s failed:\n%s', db_name, e)
            raise Exception("Couldn't drop database %s: %s" % (db_name, e))
        else:
            _logger.info('DROP DB: %s', db_name)

    fs = odoo.tools.config.filestore(db_name)
    if os.path.exists(fs):
        shutil.rmtree(fs)
    return True

def exp_dump(db_name, format):
    with tempfile.TemporaryFile() as t:
        dump_db(db_name, t, format)
        t.seek(0)
        return t.read().encode('base64')

def dump_db_manifest(cr):
    pg_version = "%d.%d" % divmod(cr._obj.connection.server_version / 100, 100)
    cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
    modules = dict(cr.fetchall())
    manifest = {
        'odoo_dump': '1',
        'db_name': cr.dbname,
        'version': odoo.release.version,
        'version_info': odoo.release.version_info,
        'major_version': odoo.release.major_version,
        'pg_version': pg_version,
        'modules': modules,
    }
    return manifest

def dump_db(db_name, stream, backup_format='zip'):
    """Dump database `db` into file-like object `stream` if stream is None
    return a file object with the dump """

    _logger.info('DUMP DB: %s format %s', db_name, backup_format)

    cmd = ['pg_dump', '--no-owner']
    cmd.append(db_name)

    if backup_format == 'zip':
        with odoo.tools.osutil.tempdir() as dump_dir:
            filestore = odoo.tools.config.filestore(db_name)
            if os.path.exists(filestore):
                shutil.copytree(filestore, os.path.join(dump_dir, 'filestore'))
            with open(os.path.join(dump_dir, 'manifest.json'), 'w') as fh:
                db = odoo.sql_db.db_connect(db_name)
                with db.cursor() as cr:
                    json.dump(dump_db_manifest(cr), fh, indent=4)
            cmd.insert(-1, '--file=' + os.path.join(dump_dir, 'dump.sql'))
            odoo.tools.exec_pg_command(*cmd)
            if stream:
                odoo.tools.osutil.zip_dir(dump_dir, stream, include_dir=False, fnct_sort=lambda file_name: file_name != 'dump.sql')
            else:
                t=tempfile.TemporaryFile()
                odoo.tools.osutil.zip_dir(dump_dir, t, include_dir=False, fnct_sort=lambda file_name: file_name != 'dump.sql')
                t.seek(0)
                return t
    else:
        cmd.insert(-1, '--format=c')
        stdin, stdout = odoo.tools.exec_pg_command_pipe(*cmd)
        if stream:
            shutil.copyfileobj(stdout, stream)
        else:
            return stdout

def exp_restore(db_name, data, copy=False):
    def chunks(d, n=8192):
        for i in range(0, len(d), n):
            yield d[i:i+n]
    data_file = tempfile.NamedTemporaryFile(delete=False)
    try:
        for chunk in chunks(data):
            data_file.write(chunk.decode('base64'))
        data_file.close()
        restore_db(db_name, data_file.name, copy=copy)
    finally:
        os.unlink(data_file.name)
    return True

def restore_db(db, dump_file, copy=False):
    assert isinstance(db, basestring)
    if exp_db_exist(db):
        _logger.info('RESTORE DB: %s already exists', db)
        raise Exception("Database already exists")

    _create_empty_database(db)

    filestore_path = None
    with odoo.tools.osutil.tempdir() as dump_dir:
        if zipfile.is_zipfile(dump_file):
            # v8 format
            with zipfile.ZipFile(dump_file, 'r') as z:
                # only extract known members!
                filestore = [m for m in z.namelist() if m.startswith('filestore/')]
                z.extractall(dump_dir, ['dump.sql'] + filestore)

                if filestore:
                    filestore_path = os.path.join(dump_dir, 'filestore')

            pg_cmd = 'psql'
            pg_args = ['-q', '-f', os.path.join(dump_dir, 'dump.sql')]

        else:
            # <= 7.0 format (raw pg_dump output)
            pg_cmd = 'pg_restore'
            pg_args = ['--no-owner', dump_file]

        args = []
        args.append('--dbname=' + db)
        pg_args = args + pg_args

        if odoo.tools.exec_pg_command(pg_cmd, *pg_args):
            raise Exception("Couldn't restore database")

        registry = odoo.modules.registry.Registry.new(db)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, SUPERUSER_ID, {})
            if copy:
                # if it's a copy of a database, force generation of a new dbuuid
                env['ir.config_parameter'].init(force=True)
            if filestore_path:
                filestore_dest = env['ir.attachment']._filestore()
                shutil.move(filestore_path, filestore_dest)

            if odoo.tools.config['unaccent']:
                try:
                    with cr.savepoint():
                        cr.execute("CREATE EXTENSION unaccent")
                except psycopg2.Error:
                    pass

    _logger.info('RESTORE DB: %s', db)

def exp_rename(old_name, new_name):
    odoo.modules.registry.Registry.delete(old_name)
    odoo.sql_db.close_db(old_name)

    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.autocommit(True)     # avoid transaction block
        _drop_conn(cr, old_name)
        try:
            cr.execute('ALTER DATABASE "%s" RENAME TO "%s"' % (old_name, new_name))
            _logger.info('RENAME DB: %s -> %s', old_name, new_name)
        except Exception, e:
            _logger.info('RENAME DB: %s -> %s failed:\n%s', old_name, new_name, e)
            raise Exception("Couldn't rename database %s to %s: %s" % (old_name, new_name, e))

    old_fs = odoo.tools.config.filestore(old_name)
    new_fs = odoo.tools.config.filestore(new_name)
    if os.path.exists(old_fs) and not os.path.exists(new_fs):
        shutil.move(old_fs, new_fs)
    return True

def exp_change_admin_password(new_password):
    odoo.tools.config.set_admin_password(new_password)
    odoo.tools.config.save()
    return True

def exp_migrate_databases(databases):
    for db in databases:
        _logger.info('migrate database %s', db)
        odoo.tools.config['update']['base'] = True
        odoo.modules.registry.Registry.new(db, force_demo=False, update_module=True)
    return True

#----------------------------------------------------------
# No master password required
#----------------------------------------------------------

@odoo.tools.mute_logger('odoo.sql_db')
def exp_db_exist(db_name):
    ## Not True: in fact, check if connection to database is possible. The database may exists
    return bool(odoo.sql_db.db_connect(db_name))

def list_dbs(force=False):
    if not odoo.tools.config['list_db'] and not force:
        raise odoo.exceptions.AccessDenied()

    if not odoo.tools.config['dbfilter'] and odoo.tools.config['db_name']:
        # In case --db-filter is not provided and --database is passed, Odoo will not
        # fetch the list of databases available on the postgres server and instead will
        # use the value of --database as comma seperated list of exposed databases.
        res = sorted(db.strip() for db in odoo.tools.config['db_name'].split(','))
        return res

    chosen_template = odoo.tools.config['db_template']
    templates_list = tuple(set(['postgres', chosen_template]))
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        try:
            db_user = odoo.tools.config["db_user"]
            if not db_user and os.name == 'posix':
                import pwd
                db_user = pwd.getpwuid(os.getuid())[0]
            if not db_user:
                cr.execute("select usename from pg_user where usesysid=(select datdba from pg_database where datname=%s)", (odoo.tools.config["db_name"],))
                res = cr.fetchone()
                db_user = res and str(res[0])
            if db_user:
                cr.execute("select datname from pg_database where datdba=(select usesysid from pg_user where usename=%s) and not datistemplate and datallowconn and datname not in %s order by datname", (db_user, templates_list))
            else:
                cr.execute("select datname from pg_database where not datistemplate and datallowconn and datname not in %s order by datname", (templates_list,))
            res = [odoo.tools.ustr(name) for (name,) in cr.fetchall()]
        except Exception:
            res = []
    res.sort()
    return res

def exp_list(document=False):
    if not odoo.tools.config['list_db']:
        raise odoo.exceptions.AccessDenied()
    return list_dbs()

def exp_list_lang():
    return odoo.tools.scan_languages()

def exp_list_countries():
    list_countries = []
    root = ET.parse(os.path.join(odoo.tools.config['root_path'], 'addons/base/res/res_country_data.xml')).getroot()
    for country in root.find('data').findall('record[@model="res.country"]'):
        name = country.find('field[@name="name"]').text
        code = country.find('field[@name="code"]').text
        list_countries.append([code, name])
    return sorted(list_countries, key=lambda c: c[1])

def exp_server_version():
    """ Return the version of the server
        Used by the client to verify the compatibility with its own version
    """
    return odoo.release.version

#----------------------------------------------------------
# db service dispatch
#----------------------------------------------------------

def dispatch(method, params):
    g = globals()
    exp_method_name = 'exp_' + method
    if method in ['db_exist', 'list', 'list_lang', 'server_version']:
        return g[exp_method_name](*params)
    elif exp_method_name in g:
        passwd = params[0]
        params = params[1:]
        check_super(passwd)
        return g[exp_method_name](*params)
    else:
        raise KeyError("Method not found: %s" % method)
