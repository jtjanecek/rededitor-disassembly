# Pokémon Mystery Dungeon: Red Rescue Team

[![build](https://github.com/pret/pmd-red/actions/workflows/build.yml/badge.svg?branch=master)](https://github.com/pret/pmd-red/actions/workflows/build.yml)

This is a decompilation of Pokémon Mystery Dungeon: Red Rescue Team.

It builds the following rom:

* pmd_red.gba `sha1: 9f4cfc5b5f4859d17169a485462e977c7aac2b89`

To set up the repository, see [INSTALL.md](INSTALL.md).

For contacts and other pret projects, see [pret.github.io](https://pret.github.io/).

## Editing Level-Up Stats via JSON

You can export/import the `lvmp###` level-up stat archives (stored in `data/system_sbin.s`) with:

```bash
./scripts/lvmp_json.py export
```

This writes JSON files to `data/monster/levelup/` (one file per `lvmp###` entry).

After editing those JSON files, re-pack them back into `data/system_sbin.s`:

```bash
./scripts/lvmp_json.py import
```

Then build the ROM normally.
