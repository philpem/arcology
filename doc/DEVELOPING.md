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
#   python3 -c 'import secrets; print(secrets.token_hex(32))'

# apply database migrations
flask db upgrade

# create an admin user
flask create-admin

# run the development server
python -m myapp
```

See also `CONTRIBUTING.md` in the project root for a more detailed guide.


## Database profiling and debugging

Database profiling requires the `sqltap` library.

It can be enabled in the config file (see `DEBUG_DB_PROFILING`).

To log all queries which are sent to the database, enable `DEBUG_DB_LOG`.


## Running under Gunicorn

`gunicorn -b 0.0.0.0:5000 'myapp.app:create_app()'`
