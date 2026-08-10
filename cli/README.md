# ngenCerf Command Line Interface (CLI) Documentation

The `ngenCerf` CLI provides a command-line interface to interact with the ngenCerf server, allowing you to create, update, manage, and run calibration jobs. It offers an alternative to the GUI, enabling automation and scripting capabilities.

## Table of Contents

- [Building](#building)
- [Login](#login)
- [Subcommands](#subcommands)
  - [archive](#archive)
  - [cancel](#cancel)
  - [delete](#delete)
  - [download](#download)
  - [export](#export)
  - [import](#import)
  - [jobs](#jobs)
  - [register](#register)
  - [run](#run)
  - [show](#show)
  - [unarchive](#unarchive)
  - [update](#update)
  - [upload-forcing](#upload-forcing)
  - [upload-geopkg](#upload-geopkg)
  - [upload-obs](#upload-obs)
- [Output Files](#output-files)
- [Importing and Exporting](#importing-and-exporting)


## Building

```
NOte: This step is not necessary when running on Parallel Works
```
Before using the CLI, it must be built from the source code. Run the following script, located in the `cli` directory to build the executable:

```bash
$ ./build_cli.sh
```

Example output:

```plaintext
$ ./build_cli.sh
==> Creating build virtual environment...
==> Upgrading pip and installing PyInstaller...
==> Installing build dependencies from pyproject.toml...
==> Running PyInstaller...
==> Build complete. Executable located at: dist/ngencerf
==> Cleaning up...
```

When complete, the executable is built in the `dist` directory. You can then copy it to a directory in your PATH, such as `~/.local/bin` or `/usr/local/bin`:

```bash
$ sudo cp dist/ngencerf /usr/local/bin
```

## Login

The first tie you use the CLI, you will be prompted for your user and password. 
This information will be saved in `~/.ngencerf_env`.  Once saved, the CLI automatically reads them.

## Subcommands

### archive

Archives one or more calibration jobs, removing them from most operations without permanently deleting them.

**Usage:**

```bash
ngencerf archive run_ids
```

**Arguments:**

- `run_ids`: A space-separated list of one or more calibration run IDs.

**Example:**

```bash
ngencerf archive 1 2 3
```

---

### cancel

Cancels a running calibration job.

**Usage:**

```bash
ngencerf cancel run_id
```

**Arguments:**

- `run_id`: The calibration run ID of the job to cancel.

**Example:**

```bash
ngencerf cancel 42
```

---

### delete

Permanently deletes one or more calibration jobs after prompting for confirmation.

**Usage:**

```bash
ngencerf delete run_ids
```

**Arguments:**

- `run_ids`: A space-separated list of one or more calibration run IDs to delete.

**Behavior:**

Upon execution, the CLI will display job details (if a single job is specified) and prompt:

```
Type 'delete' to confirm the permanent deletion of calibration jobs [run_ids]:
```

The deletion proceeds only if the user types `delete`.

**Example:**

```bash
ngencerf delete 99
```

---

### download

Downloads a ZIP archive for a calibration run.

**Usage:**

```bash
ngencerf download run_id [--output OUTPUT]
```

**Arguments:**

- `run_id`: The calibration run ID to download.
- `--output`, `-o`: (Optional) The path to save the downloaded file. Defaults to `~/Downloads`.

**Example:**

```bash
ngencerf download 123 --output ~/Downloads/job_123.zip
```

---

### export

Exports a calibration job to a JSON file or displays it on the console.

See also [import](#import) and [Importing and Exporting](#importing-and-exporting)

**Usage:**

```bash
ngencerf export run_id [--output OUTPUT] [--show]
```

**Arguments:**

- `run_id`: The calibration run ID to export.
- `--output`, `-o`: (Optional) The path to save the export file. Defaults to `~/Downloads`.
- `--show`, `-s`: (Optional) Display the job in the console.

**Example:**

```bash
ngencerf export 456 --output ./job_456.json --show
```

---

### import

Imports a new job definition from a JSON file, with an optional `--run` flag to immediately start the job.
If `--run` is specified, it overrides `run_after_import` if specified in the Json file

See also [export](#export) and [Importing and Exporting](#importing-and-exporting)

**Usage:**

```bash
ngencerf import input_file [--run true|false]
```

**Arguments:**

- `input_file`: The path to the JSON file containing the job definition.
- `--run`, `-r`: (Optional) Overrides the `run_after_import` field in the JSON file. Defaults to `true` if specified without a value.

**Example:**

```bash
ngencerf import my_job.json --run false
```

---

### jobs

Lists all calibration jobs, optionally saving the output to a markdown file.

**Usage:**

```bash
ngencerf jobs [--output OUTPUT]
```

**Arguments:**

- `--output`, `-o`: (Optional) The path to save the job list. Defaults to `~/Downloads`.

**Example:**

```bash
ngencerf jobs --output ./all_jobs.md
```

---

### register

Registers a new user for the ngenCerf server.  The credentials will be saved in ~/.ngencerf_env.  Also see [login](#login)

**Usage:**

```bash
ngencerf register [email]
```

**Arguments:**

- `email`: (Optional) The email address to register.

**Example:**

```bash
ngencerf register user@example.com
```

---

### run

Submits a calibration run for execution.

**Usage:**

```bash
ngencerf run run_id
```

**Arguments:**

- `run_id`: The calibration run ID to submit.

**Example:**

```bash
ngencerf run 789
```

---

### show

Displays the details of a calibration job.

**Usage:**

```bash
ngencerf show run_id
```

**Arguments:**

- `run_id`: The calibration run ID to display.

**Example:**

```bash
ngencerf show 456
```

---

### unarchive

Unarchives one or more calibration jobs, restoring them to active status.

**Usage:**

```bash
ngencerf unarchive run_ids
```

**Arguments:**

- `run_ids`: A space-separated list of one or more calibration run IDs.

**Example:**

```bash
ngencerf unarchive 1 2 3
```

---

### update

Updates an existing calibration job with new parameters from a JSON file.

**Usage:**

```bash
ngencerf update run_id input_file
```

**Arguments:**

- `run_id`: The calibration run ID to update.
- `input_file`: The path to the JSON file containing the updated job parameters.

**Example:**

```bash
ngencerf update 456 updated_job.json
```

---

### upload-forcing

Uploads forcing data files for a calibration run.

**Usage:**

```bash
ngencerf upload-forcing run_id forcing_dir
```

**Arguments:**

- `run_id`: The calibration run ID.
- `forcing_dir`: The directory containing forcing data files to upload.

**Example:**

```bash
ngencerf upload-forcing 456 ./forcing_data
```

---

### upload-geopkg

Uploads a GeoPackage file for a calibration run.

**Usage:**

```bash
ngencerf upload-geopkg run_id geopkg_file
```

**Arguments:**

- `run_id`: The calibration run ID.
- `geopkg_file`: The path to the GeoPackage file to upload.

**Example:**

```bash
ngencerf upload-geopkg 456 ./data.gpkg
```

---

### upload-obs

Uploads observation data files for a calibration run.

**Usage:**

```bash
ngencerf upload-obs run_id obs_dir
```

**Arguments:**

- `run_id`: The calibration run ID.
- `obs_dir`: The directory containing observation data files to upload.

**Example:**

```bash
ngencerf upload-obs 456 ./obs_data
```
# Output Files

Several subcommands save their output to disk. By default, files are saved to `~/Downloads` using a default name that varies by command.

You can override the default location and/or filename using the `--output` or `--export` option (depending on the command). The behavior is as follows:

- **Directory only**: If you specify a directory (e.g., `--output ~/foo/`), the default filename is used within that directory.
- **Filename only**: If you specify a filename without a directory (e.g., `--output new-file.txt`), the file is saved to `~/Downloads` with the given name.
- **Full path**: If you specify a full path (e.g., `--output ~/foo/new-file.txt`), the file is saved exactly at that location.

**Examples:**

- `--output ~/` → saves to `~/my-default.txt`
- `--output ~/foo/` → saves to `~/foo/my-default.txt`
- `--output new-file.txt` → saves to `~/Downloads/new-file.txt`
- `--output ~/foo/new-file.txt` → saves to `~/foo/new-file.txt`


# Importing and Exporting

The import/export facility allows you to export the configuration of an existing job and then import it to a new job after making any desired changes

Here is an example of exported data:

```
{
    "metadata": {
        "source_calibration_run_id": 269,
        "source_status": "Done",
        "time_range": {
            "start_time": "2013-01-01T00:00:00+00:00",
            "end_time": "2022-12-31T22:00:00+00:00"
        },
        "job_data_dir": "/ngencerf/data/ngen-cal-work/run_calib/269_peter",
        "num_catchments": 5
    },
    "run_after_import": false,
    "gage_id": "01055000",
    "forcing_source": "AORC",
    "observational_source": "Data Services",
    "geopackage_source": "User Upload",
    "geopackage_user_uploaded_file_path": "/ngencerf/data/ngen-cal-work/run_calib/318_peter/geopackage/gauge_01123000.gpkg",
    "modules": [
        "T-Route",
        "CFE-S",
        "Noah-OWP-Modular"
    ],
    "formulation_name": "noah_cfes_troute_01055000_clone",
    "use_sloth": false,
    "sloth_parameters": [],
    "automatic_validation": true,
    "calibration_times": {
        "calibration_start_time": "2016-10-01T00:00:00Z",
        "calibration_end_time": "2017-09-01T00:00:00Z",
        "simulation_start_time": "2015-10-01T00:00:00Z",
        "simulation_end_time": "2017-09-01T00:00:00Z"
    },
    "validation_times": {
        "validation_start_time": "2015-10-01T00:00:00Z",
        "validation_end_time": "2016-09-01T00:00:00Z",
        "simulation_start_time": "2014-10-01T00:00:00Z",
        "simulation_end_time": "2017-09-01T00:00:00Z"
    },
    "streamflow_threshold": 3.88,
    "peak_flow_threshold": null,
    "parameters": [
        {
            "name": "b",
            "minimum": 2.0,
            "maximum": 15.0,
            "initial_value": 4.05,
            "module": "CFE-S"
        }
    ],
    "objective_function": "KGE",
    "optimization_inputs": [
        {
            "name": "r",
            "value": 0.2
        }
    ],
    "optimization": "DDS",
    "save_plot_iteration_frequency": 1,
    "save_output_iteration": false,
    "stop_criteria": 5
}
```


The format for exported and imported data is the same.  The metadata section on export contains data that, while useful, is not needed for import.
You can also use the metadata section for your own information, such as comments.  It will be ignored on import.

| Field                     | Description                                                                                                                                                            |
|---------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| source_calibration_run_id | Metadata: ID of the job that was exported.                                                                                                                             |
| time_range                | Metadata: Intersection of time range from the forcing data and observation data, constraining calibration and validation.                                              |
| errors                    | Metadata: Shows any outstanding errors in the job that would prevent it from running.                                                                                  |
| run_after_import          | Flag indicating whether to submit the job immediately provided all required information is available and there are no errors. Can be overridden by the `--run` option. |
| gage_id                   | The gage_id associated with the calibration job.                                                                                                                       |
| forcing_source            | The source of the forcing data.  (e.g., AORC or User Upload)                                                                                                           |

---

The paths specified for forcing, observational, and geopackage data do not refer to the user's local machine, but rather are paths on the **server machine**.  
They are obtained from the job which was exported and normally should not be changed, except to clear them out.

---

| Field                                | Description                                                                                                            |
|--------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| forcing_user_uploaded_dir_path       | The location of the forcing data uploaded by the user to the exported job.                                             |
| observational_source                 | The source of the observational data (e.g., Data Services or User Upload)                                              |
| observational_user_uploaded_dir_path | The location of the observational data uploaded by the user.                                                           |
| geopackage_source                    | The source of the geopackage data (e.g., Data Services, User Upload).                                                  |
| geopackage_user_uploaded_file_path   | The location of the geopackage file uploaded by the user.                                                              |
| modules                              | The list of modules used for this calibration.                                                                         |
| formulation_name                     | User-supplied formulation name that has no effect on the job.                                                          |
| use_sloth                            | Flag indicating whether the SLoTH model is used. If true, `sloth_parameters` must be provided.                         |
| sloth_parameters                     | Required if `use_sloth` is true. The SLoTH parameter definitions. All fields are required for each SLoTH variable.     |
| automatic_validation                 | If true, then a validation is run automatically after the calibration run.      (Should we still be exporting this?)   |
| calibration_times                    | The time ranges to use for calibration.                                                                                |
| validation_times                     | The time ranges to use for validation (required if `automatic_validation` is true). Must be outside the calibration times. |
| stream_flow_threshold                | Required if a categorical objective function is specified. If empty, ngen-cal will not calculate categorical metric.   |
| peak_flow_threshold                  | Required if an event-based objective function is specified. If empty, ngen-cal will not calculate event-based metric.  |
| parameters                           | Module parameters to use for calibration tuning including name, min, max, and module. All fields are required.         |
| objective_function                   | Metric to use for the objective function.                                                                              |
| optimization                         | Optimization algorithm (DDS, GWO, PSO).                                                                                |
| optimization_inputs                  | Inputs for the selected optimization.                                                                                  |
| save_plot_iteration_frequency        | How often ngen-cal will generate plots during the calibration.                                                         |
| save_output_iteration                | If true, output for each iteration is saved in separate files (not supported by UI).                                   |
| stop_criteria                        | Number of worker iterations to run.                                                                                    |


Note from Peter: Need to finish editing this, based on what we have in the current confluence page
---
