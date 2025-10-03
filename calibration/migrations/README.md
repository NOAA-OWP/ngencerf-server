# Migration Notes

## ⚠️ Migration Notice: `0033b_prepare_forecastcycle_for_rename`

A new migration **0033b_prepare_forecastcycle_for_rename** was added to clean up legacy `ForecastCycle` rows
before the model/table rename in **0034_rename_forecastcycle_to_forecastconfiguration**.

### Impact

- **Fresh databases**: No issues. Migrations apply in correct order:
  ```
  0031 → 0032 → 0033 → 0033b → 0034 → 0035 → 0036
  ```

- **Databases currently at 0029, 0030, 0031, 0032, or 0033**:
  No issues. Running `migrate` will apply migrations in order, including 0033b before 0034.

- **Databases that already migrated past 0034 before 0033b was introduced**:
  You may see an error like:
  ```
  InconsistentMigrationHistory: Migration calibration.0034_rename_forecastcycle_to_forecastconfiguration is applied before its dependency calibration.0033b_prepare_forecastcycle_for_rename
  ```

  #### Fix:
  Manually mark 0033b as applied (safe, because the cleanup only deletes legacy rows you already don’t have):

  ```sql
  INSERT INTO django_migrations (app, name, applied)
  VALUES ('calibration', '0033b_prepare_forecastcycle_for_rename', NOW());
  ```

  After that, `showmigrations` should list 0033b as applied, and `migrate` will run cleanly.

### Notes
- Fresh DBs and older DBs (pre-0034) do not need any manual steps.
- Only DBs that had already applied 0034+ before this change require the one-time manual SQL insert above.


