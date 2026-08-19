---
Title: dada
Date: 2026-07-13 12:42:25.034175+00:00
Tags: system-vault, agents, editors, documentation, configuration, update-script, versioning
Summary: The system vault is the single, designated repository for Tzara’s help documentation, editors, and agents, configured via the `SYSTEM_VAULT` setting. It is seeded on creation and can be updated with the provided `refresh-docs` scripts (which require Tzara to be running), with changes being revertable if versioning is enabled.
---

# Dada Home

This is your **system vault**: [agent](http://localhost:8000/agents) definitions, [editor](http://localhost:8000/editors) definitions, and [[help]] docs live here.

# What is a System Vault?

This is a designated special vault which stores your help documentation, editors, and agents, if any.  The system vault is the **only vault that allows for defining agents or editors**, and the location can be configured as `SYSTEM_VAULT` in either your environment (`.env` file) or in `config.py`.  As such, there is **only one** system vault. 

This vault gets seeded once on creation.  You can see all your files by visiting [Index](/index/{{vault}}).

If you upgrade Tzara after install via a `git pull`, then these documents will not be updated.  There is a helper script for updating documents, and optionally any example agents or editors.  Run either `refresh-docs.bat` on Windows or `refresh-docs.sh` on Linux/Mac.  Tzara nees to be running for the update scripts to work, or you can manully update files as you see fit by copying from `/app/seed/system/` to wherever you've stored your system vault on disk.  Running the script does nothing but tell you what might need updating.  The command `refresh-docs.bat --help` or `refresh-docs.sh --help` for details. Any parameters specified for the .bat or .sh file get passed directly to the `refresh_seed_docs.py` script, e.g. `refresh-docs.bat --apply`.

If you have versioning turned on, then any changes made by the update script are revertable. 
