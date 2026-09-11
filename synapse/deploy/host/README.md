# Hosting several applications on one VPS

Run one Caddy proxy for the whole host and a separate Compose project for each application. The proxy owns ports 80 and 443. Synapse has its own database volume, collector credential, CPU/memory limits, and frontend network. Unrelated applications do not need to be part of this repository.

Suggested directories:

- `/opt/web-proxy`: host proxy configuration, certificates, site files, and staff password hashes.
- `/opt/apps/synapse-player-atlas`: this repository.
- `/opt/apps/another-app`: another application and its own Compose project.

Do not run `synapse/deploy/compose.yaml` alongside this setup: that file includes a proxy for a dedicated single-application host. Use `compose.shared.yaml` for Synapse instead.

## First setup

Install Docker Engine and Compose from Docker's official repository. Create the shared proxy directory and copy this folder's `compose.yaml` and `Caddyfile` into it. Create its `sites` and `secrets` directories. Keep `secrets` readable only by the host administrator.

Create Synapse's frontend network once:

```sh
sudo docker network create synapse_web
```

Under `/opt/apps/synapse-player-atlas/synapse/deploy`, create `.env` with `MONOSUITE_SERVER_ID`. Prepare `secrets/collector/monosuite.token` as described in the main guide. The credential directory and file need UID 10001 ownership and modes 700 and 600 respectively. No domain is required by `compose.shared.yaml`.

```sh
sudo docker compose -f compose.shared.yaml build
sudo docker compose -f compose.shared.yaml run --rm collector check-schema
sudo docker compose -f compose.shared.yaml up -d
```

Then start the proxy from `/opt/web-proxy`:

```sh
sudo docker compose up -d
```

With no site configured, public HTTP returns 404. Synapse is bound to the VPS loopback address on port 8787 and is reachable through SSH forwarding. For example, using an SSH key already authorized for your account:

```sh
ssh -N -L 8790:127.0.0.1:8787 ubuntu@YOUR_VPS_IP
```

Open http://127.0.0.1:8790 on that computer. The SSH tunnel provides access control; port 8787 is not publicly exposed. Choose another local port if 8790 is occupied.

## Enable MonoSuite sign-in

Follow [the OAuth deployment guide](../OAUTH.md). It adds a separate credential mount and session volume to the viewer, with an explicit staff approval list. Set `SYNAPSE_REQUIRE_AUTH=true` and include `-f compose.oauth.yaml` alongside `-f compose.shared.yaml` in subsequent application operations. Collection and other hosted applications are unaffected.

## Give Synapse its domain

Point the domain's DNS A record at the VPS. Only add an AAAA record if IPv6 routing and the firewall have also been checked. Allow inbound TCP 80 and 443.

Generate each staff password hash with `sudo docker run --rm -it caddy:2-alpine caddy hash-password`. Put one username and hash per line in `/opt/web-proxy/secrets/synapse-users.caddy`. All of these users can read all Synapse data, including private messages.

Copy `synapse.caddy.example` into `/opt/web-proxy/sites/synapse.caddy` and replace `atlas.example.com` with the real domain. Keep the authentication block in place until another server-side access control has been implemented. From `/opt/web-proxy`:

```sh
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Caddy obtains and renews the certificate. Verify that an unauthenticated request to both `/` and `/api/graph` gets 401, then test the site with a staff account.

## Add an unrelated application

Give the application its own Compose project name, directory, volumes, and secrets. Create a separate frontend network, such as `otherapp_web`, and attach only its HTTP frontend and the shared proxy. Keep its databases and workers on its private application network. Do not publish another application's containers directly on host ports 80 or 443.

Add that external network to `/opt/web-proxy/compose.yaml` and to the Caddy service's network list. Add a site file in `/opt/web-proxy/sites/` that sends its domain to the application's distinct Docker network alias and internal port. Use whatever authentication that application requires; Synapse's staff password file is not a host-wide login policy.

Run `sudo docker compose up -d` in `/opt/web-proxy` after changing network attachments, then validate and reload Caddy. Restarting an application's Compose project does not stop other apps. Recreating the shared proxy can briefly interrupt all websites, so group proxy changes together.

## Operations

Synapse's viewer and collector are each limited to 768 MiB and one CPU; the proxy is limited to 256 MiB and half a CPU. These are ceilings, not reserved resources. Revisit them as data grows. A small VPS cannot host an unlimited number of busy apps.

Use `sudo docker compose -f compose.shared.yaml ps` and `logs --tail 50` from Synapse's deployment directory. Backup commands from the main guide work with the same `-f compose.shared.yaml` option. Keep backups off the VPS and test restoration. Avoid `down -v`, which removes a project's database volume. The externally managed frontend network is separate from that volume.

The proxy never mounts Synapse's database or collector token. This arrangement separates applications operationally; it is not a security boundary for hostile tenants who have root access or access to the Docker socket.
