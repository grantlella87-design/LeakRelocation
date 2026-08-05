# LeakRelocation

Leak relocation workflow for DNV / GeoPandas production processing.

## Key rule

Use decoded `ASSETGROUP + ASSETTYPE` for pipe material classification. Do not use the DNV `material` Grade field as the material class for relocation assessment.

## Local workflow

Current working files are synchronized from:

- `C:\Users\lellag\Downloads\LeakRelocation-GeoPandas`
- `\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation`

Run PowerShell helper scripts from the `powershell/` folder.
