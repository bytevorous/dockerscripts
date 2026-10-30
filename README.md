# dockerscripts
Shortcuts to make container management easier.

## Backup and restore

`docker-backup.py DEST` writes `DEST/docker-backup-<host>-<time>/` with every container's config, named
volumes, networks, reinit scripts and checksums (`--no-binds` when host folders stay in place).

`docker-restore.py BACKUP` puts it back: verify checksums → load images → create networks →
create/fill volumes → recreate containers in dependency order. Anything already on the host is kept;
replace a corrupt copy by naming it:

    python3 docker-restore.py BACKUP --dry-run
    python3 docker-restore.py BACKUP
    python3 docker-restore.py BACKUP --overwrite-volume moodle_data --overwrite-container moodle
    python3 docker-restore.py BACKUP --overwrite-all-volumes --overwrite-all-containers   # full rollback
    python3 docker-restore.py BACKUP --only mariadb --no-start

Both `--overwrite-*` flags are repeatable. Anything not in the backup (or whose backup failed) is linked to
what is on the host now and never emptied or removed; the run ends with a "Linked to existing" list.
Containers whose bind source paths are missing on the host
are skipped (`--allow-missing-binds` to create them anyway). Run as root on the docker host, or over
ssh: `ssh user@server 'python3 - BACKUP' < docker-restore.py`.
