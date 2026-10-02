# The console database

This is on `main`. It is not in the `v2026.10.03` binary.

The console runs on a dedicated Linux server. That server is the deploy host. The deploy host is the machine that installs the cloud, and it stays outside the cluster. The cluster is the Kubernetes and OpenStack cloud this console builds. Users, jobs, and saved secrets for the console itself stay on the deploy host. They are not stored in the cluster.

By default that data is in SQLite. SQLite is a database kept in one file on the deploy host. A new install uses `data/console.db`. The `database_url` key in `config.yaml` is `sqlite:///./data/console.db`. Postgres is the other supported database. Postgres is a database server the console can open instead of that file. A Postgres URL starts with `postgresql+psycopg://`.

`database_url` is what the console opens when the process starts. That process keeps the connection it opened. Move does not switch it. You restart afterward: stop the console and start it again. Until you do, the page is still on the old database.

Move copies every table to the database you name. It creates the schema on the target, copies the rows, and writes `database_url`. The target must have no rows. A database that already has data is refused, and `config.yaml` is left unchanged. Other lines in `config.yaml`, including comments, stay as they are. The copy includes every row, including encrypted secrets. Protect the target the same way you protect the database you copied from. Keep `config.yaml` with that database. The file holds the key those secrets were encrypted with.

A platform admin is a user who is not limited to one tenant. The API is for a platform admin. `GET /api/v1/database` returns the engine, `sqlite` or `postgresql`, and the URL with the password replaced by `***`. `POST /api/v1/database/move` takes `{"target_url": "..."}`. The password is not returned. The response says `restart_required`. MySQL is refused. The URL this process already has open is refused.

The Admin page has a Database card for those two calls. The target URL goes in a password field. The field is cleared when you submit it. The browser does not store it.
