# OpenRC support

`run.sh` can install and enable Nighty as a native OpenRC service:

```sh
bash scripts/run.sh autostart
```

It detects OpenRC through `rc-update` and `openrc-run`, writes `/etc/init.d/nighty`,
enables it in the `default` runlevel, and starts it immediately. The generated
service uses `supervise-daemon`, so the complete stack is restarted after an
unexpected exit. It also waits for the network and writes service output to:

```text
$NIGHTY_DIAG_DIR/service.log
```

The generated service runs as the invoking user rather than root. This is
important because the Wine prefix, instance lock, and diagnostics belong to that
user. OpenRC installations must provide `supervise-daemon` (OpenRC 0.21 or
newer); otherwise the installer refuses to create a service that would not
restart reliably.

## Manual installation

The generated service is preferred because it fills in the actual user and
repository path. For a manual installation, copy [`openrc/nighty`](../openrc/nighty)
to `/etc/init.d/nighty`, replace `<USER>` and `<REPO_DIR>`, then run:

```sh
sudo chmod +x /etc/init.d/nighty
sudo rc-update add nighty default
sudo rc-service nighty start
```

Check status and logs with:

```sh
sudo rc-service nighty status
tail -f diagnostics/service.log
```

Remove it with:

```sh
sudo rc-service nighty stop
sudo rc-update del nighty default
sudo rm -f /etc/init.d/nighty
```
