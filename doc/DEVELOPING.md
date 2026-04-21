# Developer notes

## Running in development mode:

```
python -m venv venv
. venv/bin/activate
pip3 install -r requirements.txt
cp myapp/myapp.cfg.example myapp/myapp.cfg

# edit myapp.cfg -- specifically change the secret key and enable a
# database backend.
#
# to generate a secret key:
#   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'

# apply database migrations
flask db upgrade

# create an admin user
flask create-admin

# run the development server
python -m myapp
```

See also `CONTRIBUTING.md` in the project root for a more detailed guide.


## Database migrations: adding values to a PostgreSQL enum

`ALTER TYPE ... ADD VALUE` **cannot run inside a transaction**. Alembic wraps
all migrations in a transaction by default, so using `bind.execute()` directly
will appear to succeed (Alembic stamps the revision) but the new enum value
will **not** actually be persisted in the database.

Set `autocommit = True` at module level in the migration file to run it outside
any transaction block:

```python
# At module level, after depends_on:
autocommit = True

def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text("ALTER TYPE myenum ADD VALUE IF NOT EXISTS 'NEW_VALUE'"))
```

If a migration was already stamped but the enum value is missing, roll the
stamp back to the previous revision and re-run the upgrade:

```bash
flask db stamp <previous_revision_id>
flask db upgrade
```

## Switching branches when your branch has migrations

The database schema is independent of your git branch. Before switching away
from a branch that added migrations, downgrade the DB first.  Run the script
to get the exact command (local and Docker variants are both printed):

```bash
python devtools/db_branch_switch.py          # compare against master
python devtools/db_branch_switch.py <branch> # compare against another branch
```

Then run the printed command, switch branches, and `flask db upgrade` if needed.
See `doc/BRANCH_DB_SWITCHING.md` for the full workflow and edge cases.

## Database profiling and debugging

Database profiling requires the `sqltap` library.

It can be enabled in the config file (see `DEBUG_DB_PROFILING`).

To log all queries which are sent to the database, enable `DEBUG_DB_LOG`.


## Running under Gunicorn

`gunicorn -b 0.0.0.0:8000 'myapp.app:create_app()'`
