# Legacy in-place patch scripts — do not run

These scripts edit the production copy of the relocation script on the shared
drive in place:

    \\ngusnasnwh001\...\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py

Each takes a backup, then appends or rewrites code — appending a redefinition
of `request_json` after `main()`, appending a second `build_out_fields`, and so
on. Running them repeatedly is what left the script with five functions defined
twice, where Python silently kept only the last definition of each.

**The changes these scripts applied are now part of the source** in
`src/leak_relocation_geopandas.py`:

| Script | What it injected | Where it lives now |
| --- | --- | --- |
| `patch_request_json_append_assettype.py` | `request_json` wrapper adding `ASSETGROUP,ASSETTYPE` to pipe-layer `outFields` | `apply_pipe_domain_out_fields()` |
| `patch_include_assetgroup_assettype.py` | domain fields in `build_out_fields` | `build_out_fields()` |
| `patch_force_assetgroup_assettype_at_outfields.py` | same, via a `locals()` rewrite that never worked | superseded |

A `Patch-LeakRelocation-NativeCRS.ps1` did the same for per-layer native CRS
handling; that code is now the native CRS block at the top of the module, and
the script has been deleted along with the rest of the PowerShell.

Re-running any of them would append those definitions a second time and
re-create the duplication.

They are kept only as a record of what was applied to production. To change
the workflow now, edit `src/leak_relocation_geopandas.py`, run the tests, and
deploy the file — do not patch the deployed copy.
