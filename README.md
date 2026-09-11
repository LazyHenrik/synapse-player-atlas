# Synapse Player Atlas

A read-only player graph and shared log explorer for Garry's Mod communities administered through MonoSuite.

The collector samples co-presence and imports gameplay and administrative records using the bundled `monosuite_cli` client. The viewer supports time and connection filters, player detail, and log searches across pairs or groups of up to 20 players. Collection resumes after restarts and reports missing or capped log windows.

## Try it locally

Requires Python 3.11 or newer. From this directory:

```sh
python -m pip install -r synapse/requirements.txt
python -m synapse demo --db demo.sqlite
python -m synapse serve --db demo.sqlite
```

Open http://127.0.0.1:8787 after demo generation finishes. The demo contains fictional players and records.

## Collect and deploy

See the [setup, interpretation, and deployment guide](synapse/README.md) for credentials, real collection, weighting, API traps, backups, and the supplied Docker/Caddy deployment. The stack supports a private shared staff website with HTTPS. Each authorized staff login can access all collected content, including private messages.

MonoSuite OAuth sign-in is not implemented. The guide records the application/API-key investigation and the remaining integration work. The Docker deployment needs a smoke test on its target Linux host.

## Verify

```sh
python -m unittest discover -s tests -v
node --check synapse/static/app.js
```

No credentials or collected player databases are included. The existing client is bundled in `monosuite_cli.py`; its original documentation is in [CLI-README.md](CLI-README.md). Synapse's collector only permits its fixed read queries, even though the general-purpose CLI also exposes administrative actions.

The included Project: Synapse logo and wordmark are community branding, separate from the [code license](LICENSE).
