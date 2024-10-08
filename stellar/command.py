import textwrap
import sys
from datetime import datetime
from time import sleep

import humanize
import click
import logging
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError, OperationalError

from .app import Stellar, __version__
from .config import InvalidConfig, MissingConfig, load_config, save_config
from .operations import database_exists, list_of_databases, SUPPORTED_DIALECTS


def get_app():
    app = Stellar()
    return app


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def stellar():
    """Fast database snapshots for development. It's like Git for databases."""
    pass


@stellar.command()
def version():
    """Shows version number"""
    click.echo("Stellar %s" % __version__)


@stellar.command()
def gc():
    """Deletes old stellar tables that are not used anymore"""
    def after_delete(database):
        click.echo("Deleted table %s" % database)

    app = get_app()
    app.delete_orphan_snapshots(after_delete)


@stellar.command()
@click.argument('name', required=False)
def snapshot(name):
    """Takes a snapshot of the database"""
    app = get_app()
    name = name or app.default_snapshot_name

    if app.get_snapshot(name):
        click.echo("Snapshot with name %s already exists" % name)
        sys.exit(1)
    else:
        def before_copy(table_name):
            click.echo("Snapshotting database %s" % table_name)
        app.create_snapshot(name, before_copy=before_copy)


@stellar.command()
def list():
    """Returns a list of snapshots"""
    snapshots = get_app().get_snapshots()

    click.echo('\n'.join(
        '%s: %s' % (
            s.snapshot_name,
            humanize.naturaltime(datetime.utcnow() - s.created_at)
        )
        for s in snapshots
    ))


@stellar.command()
@click.argument('name', required=False)
def restore(name):
    """Restores the database from a snapshot"""
    app = get_app()

    if not name:
        snapshot = app.get_latest_snapshot()
        if not snapshot:
            click.echo(
                "Couldn't find any snapshots for project %s" %
                load_config()['project_name']
            )
            sys.exit(1)
    else:
        snapshot = app.get_snapshot(name)
        if not snapshot:
            click.echo(
                "Couldn't find snapshot with name %s.\n"
                "You can list snapshots with 'stellar list'" % name
            )
            sys.exit(1)

    # Check if slaves are ready
    if not snapshot.slaves_ready:
        if app.is_copy_process_running(snapshot):
            sys.stdout.write(
                'Waiting for background process(%s) to finish' %
                snapshot.worker_pid
            )
            sys.stdout.flush()
            while not snapshot.slaves_ready:
                sys.stdout.write('.')
                sys.stdout.flush()
                sleep(1)
                app.db.session.refresh(snapshot)
            click.echo('')
        else:
            click.echo('Background process missing, doing slow restore.')
            app.inline_slave_copy(snapshot)

    app.restore(snapshot)
    click.echo('Restore complete.')


@stellar.command()
@click.argument('name')
def remove(name):
    """Removes a snapshot"""
    app = get_app()

    snapshot = app.get_snapshot(name)
    if not snapshot:
        click.echo("Couldn't find snapshot %s" % name)
        sys.exit(1)

    click.echo("Deleting snapshot %s" % name)
    app.remove_snapshot(snapshot)
    click.echo("Deleted")


@stellar.command()
@click.argument('old_name')
@click.argument('new_name')
def rename(old_name, new_name):
    """Renames a snapshot"""
    app = get_app()

    snapshot = app.get_snapshot(old_name)
    if not snapshot:
        click.echo("Couldn't find snapshot %s" % old_name)
        sys.exit(1)

    new_snapshot = app.get_snapshot(new_name)
    if new_snapshot:
        click.echo("Snapshot with name %s already exists" % new_name)
        sys.exit(1)

    app.rename_snapshot(snapshot, new_name)
    click.echo("Renamed snapshot %s to %s" % (old_name, new_name))


@stellar.command()
@click.argument('name')
def replace(name):
    """Replaces a snapshot"""
    app = get_app()

    snapshot = app.get_snapshot(name)
    if not snapshot:
        click.echo("Couldn't find snapshot %s" % name)
        sys.exit(1)

    app.remove_snapshot(snapshot)
    app.create_snapshot(name)
    click.echo("Replaced snapshot %s" % name)


@stellar.command()
@click.argument('url', required=False)
@click.argument('project', required=False)
def init(url, project):
    """Initializes Stellar configuration."""

    def prompt_url():
        msg = textwrap.dedent("""\
            Please enter the url for your database.

            For example:
            PostgreSQL: postgresql://localhost:5432/
            MySQL: mysql+pymysql://root@localhost/
            """)
        return click.prompt(msg)

    while True:
        if not url:
            url = prompt_url()

        if url.count('/') == 2 and not url.endswith('/'):
            url = url + '/'

        # if (
        #     url.count('/') == 3 and
        #     url.endswith('/') and
        #     url.startswith('postgresql://')
        # ):
        #     connection_url = url + 'template1'
        # else:
        #     connection_url = url
        connection_url = url

        try:
            engine = create_engine(connection_url, echo=False)
        except ArgumentError as err:
            click.echo("Error: %s" % err)
            url = None
            continue

        try:
            conn = engine.connect()
        except OperationalError as err:
            click.echo("Could not connect to database: %s" % url)
            click.echo("Error message: %s" % err)
            click.echo('')
        else:
            break

        url = None

    if engine.dialect.name not in SUPPORTED_DIALECTS:
        click.echo("Your engine dialect %s is not supported." % (
            engine.dialect.name
        ))
        click.echo("Supported dialects: %s" % (
            ', '.join(SUPPORTED_DIALECTS)
        ))

    if url.count('/') == 3 and url.endswith('/'):
        while True:
            click.echo("You have the following databases: %s" % ', '.join([
                db for db in list_of_databases(conn)
                if not db.startswith('stellar_')
            ]))

            db_name = click.prompt(
                "Please enter the name of the database (eg. projectdb)"
            )
            if database_exists(conn, db_name):
                break
            else:
                click.echo("Could not find database %s" % db_name)
                click.echo('')
    else:
        db_name = url.rsplit('/', 1)[-1]
        url = url.rsplit('/', 1)[0] + '/'

    if project is None:
        project = click.prompt(
            'Please enter project name (used internally, eg. %s)' % db_name,
            default=db_name
        )

    raw_url = url

    if engine.dialect.name == 'postgresql':
        raw_url = raw_url + db_name

    with open('stellar.yaml', 'w') as project_file:
        project_file.write(
            textwrap.dedent("""\
                project_name: {name}
                tracked_databases: ['{db_name}']
                url: '{raw_url}'
                stellar_url: '{url}stellar_data'
                """)
            .format(name=project, db_name=db_name, raw_url=raw_url, url=url))

    click.echo("Wrote stellar.yaml")
    click.echo('')
    if engine.dialect.name == 'mysql':
        click.echo("Warning: MySQL support is still in beta.")
    click.echo("Tip: You probably want to take a snapshot: stellar snapshot")


def main():
    try:
        stellar()
    except MissingConfig:
        click.echo("You don't have stellar.yaml configuration yet.")
        click.echo("Initialize it by running: stellar init")
        sys.exit(1)
    except InvalidConfig as e:
        click.echo("Your stellar.yaml configuration is wrong: %s" % e.message)
        sys.exit(1)
    except ImportError as e:
        libraries = {
            'psycopg2': 'PostgreSQL',
            'pymysql': 'MySQL',
        }
        for library, name in libraries.items():
            if 'No module named' in str(e) and library in str(e):
                click.echo(
                    "Python library %s is required for %s support." %
                    (library, name)
                )
                click.echo("You can install it with pip:")
                click.echo("pip install %s" % library)
                sys.exit(1)
            elif 'No module named' in str(e) and 'MySQLdb' in str(e):
                click.echo(
                    "MySQLdb binary drivers are required for MySQL support. "
                    "You can try installing it with these instructions: "
                    "http://stackoverflow.com/questions/454854/no-module-named"
                    "-mysqldb"
                )
                click.echo('')
                click.echo("Alternatively you can use pymysql instead:")
                click.echo("1. Install it first: pip install pymysql")
                click.echo(
                    "2. Specify database url as "
                    "mysql+pymysql://root@localhost/ and not as "
                    "mysql://root@localhost/"
                )
                sys.exit(1)
        raise

if __name__ == '__main__':
    main()
