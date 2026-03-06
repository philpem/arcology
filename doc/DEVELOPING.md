# Developer notes

## Running in development mode:

```
python -m venv venv
. venv/bin/activate
pip3 install -r requirements.txt
cp regsys/regsys.cfg.example regsys/regsys.cfg

# edit regsys.cfg -- specifically change the secret key and enable a
# database backend.
#
# to generate a secret key:
#   dd if=/dev/urandom count=32k status=none | sha256sum | awk '{ print $1 }'

# initialise the database
python3 install.py

export FLASK_ENV=development
export FLASK_APP=regsys
flask run
```


## Database migrations: adding values to a PostgreSQL enum

`ALTER TYPE ... ADD VALUE` **cannot run inside a transaction**. Alembic wraps
all migrations in a transaction by default, so using `bind.execute()` directly
will appear to succeed (Alembic stamps the revision) but the new enum value
will **not** actually be persisted in the database.

Always use an autocommit connection for these statements:

```python
def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        conn = bind.execution_options(isolation_level='AUTOCOMMIT')
        conn.execute(sa.text("ALTER TYPE myenum ADD VALUE IF NOT EXISTS 'NEW_VALUE'"))
```

If a migration was already stamped but the enum value is missing, roll the
stamp back to the previous revision and re-run the upgrade:

```bash
flask db stamp <previous_revision_id>
flask db upgrade
```

## Database profiling and debugging

Database profiling requires the `sqltap` library.

It can be enabled in the config file (see `DEBUG_DB_PROFILING`).

To log all queries which are sent to the database, enable `DEBUG_DB_LOG`.


## Running under Gunicorn

`gunicorn -b 0.0.0.0:5000 regsys.app`

