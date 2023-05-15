import argparse
import asyncio
import copy
from collections import defaultdict
from getpass import getpass
import glob
import logging
import os
import pathlib
import requests
import shlex
import shutil
import subprocess
import sys
from toml import load

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.NOTSET)
logging.basicConfig(level=logging.INFO)
logging.getLogger('asyncio').setLevel(logging.WARNING)

def get(connection, url, **kwargs):
    r = connection.get(url, **kwargs)
    r.raise_for_status()
    return r

def get_project_subject_session(connection, host, session):
    """Get project ID and subject ID from session JSON
    If calling within XNAT, only session is passed"""
    r = get(
        connection,
        host + "/data/experiments/%s" % session,
        params={"format": "json", "handler": "values", "columns": "project,subject_ID,label"},
    )
    sessionValuesJson = r.json()["ResultSet"]["Result"][0]
    project = sessionValuesJson["project"]
    subjectID = sessionValuesJson["subject_ID"]

    r = get(
        connection,
        host + "/data/subjects/%s" % subjectID,
        params={"format": "json", "handler": "values", "columns": "label"},
    )
    subject = r.json()["ResultSet"]["Result"][0]["label"]

    return project, subject

def prepare_path_prefixes(project, subject):
    # get PI from project name
    pi_prefix = project.split("_")[0]

    # Paths to export source data in a BIDS friendly way
    study_prefix = "study-" + project.split("_")[1]

    return pi_prefix.lower(), study_prefix.lower()

def set_logging_level(x2b_arglist: list):
    if "--verbose"  in x2b_arglist:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

def fetch_latest_version():
    list_of_versions = glob.glob('/gpfs/data/bnc/simgs/brownbnc/*') 
    latest_version = max(list_of_versions, key=os.path.getctime)
    return (latest_version.split('-')[-1].replace('.sif', ''))

def extract_params(param, value):
    arg = []
    # if includeseq or skipseq parameter, check whether a
    # range is specified (a string with a dash), and parse
    # accordingly
    if param in ['includeseq','skipseq'] and type(value)==str:
        val_list = value.replace(" ", "").split(",")

        for val in val_list:
            if "-" in val:
                startval,stopval = val.split("-")
                expanded_val = list(range(int(startval),int(stopval)+1))
                for v in expanded_val:
                    arg.append(f"--{param} {v}") 
            else:
                arg.append(f"--{param} {val}")

    else:
        for v in value:
            arg.append(f"--{param} {v}")

    return ' '.join(arg)

def add_job_name(slurm_param_list, new_job_name):
    # Check if --job-name is already in the list
    for i, param in enumerate(slurm_param_list):
        if param.startswith("--job-name"):
            # If --job-name is already in the list, replace it
            slurm_param_list[i] = f"--job-name {new_job_name}"
            return
    # If --job-name is not in the list, append it
    slurm_param_list.append(f"--job-name {new_job_name}")

def fetch_job_ids():
    jobs = []
    with open("x2b_launched_jobs.txt", "r") as f:
        for line in f:
            job_id = line.replace("Submitted batch job ", "" ).strip()
            jobs.append(job_id)
    return jobs

def merge_config_files(user_cfg, default_cfg):
        user_slurm = user_cfg['slurm-args']
        default_slurm = default_cfg['slurm-args']
        default_x2b = default_cfg['xnat2bids-args']

        if "xnat2bids-args" in user_cfg:
            user_x2b = user_cfg['xnat2bids-args']

        # Assemble merged dictionary with default values.
        merged_dict = defaultdict(dict)
        merged_dict['xnat2bids-args'].update(default_x2b)
        merged_dict['slurm-args'].update(default_slurm)

        # Update merged dictionary with user provided arguments.
        merged_dict['slurm-args'].update(user_slurm)

        if "xnat2bids-args" in user_cfg:
            merged_dict['xnat2bids-args'].update(user_x2b)
        
        # Add session specific parameter blocks
        for key in user_cfg.keys():
            if key == 'slurm-args' or key == 'xnat2bids-args':
                continue
            merged_dict[key].update(user_cfg[key])

        return merged_dict

def parse_x2b_params(xnat2bids_dict, session, bindings):
        x2b_param_list = []
        param_lists = ["includeseq", "skipseq"]
        positional_args = ["sessions", "bids_root"]

        # Run entire xnat2bids pipeline by default
        xnat_tools_cmd = "xnat2bids"
        # Handle positional argments SESSION and BIDS_ROOT
        x2b_param_list.append(session)
        
        if "bids_root" in xnat2bids_dict:
            bids_root = xnat2bids_dict["bids_root"]
            arg = f"{bids_root}"
            bindings.append(arg)
            x2b_param_list.append(arg)

        for param, value in xnat2bids_dict.items():
            if value == "" or value is  None:
                continue
            # Extract bidsmap parameter
            elif param == "bidsmap-file":
                arg = f"--{param} {value}"
                bindings.append(value)
                x2b_param_list.append(arg)
            # Set as many verbose flags as specified.
            elif param == "verbose":
                arg = f"--{param}"
                for i in range(value):
                    x2b_param_list.append(arg)
            # If overwrite is equal to true, set flag.
            elif param == "overwrite":
                arg = f"--{param}"
                if value == True: x2b_param_list.append(arg) 
            elif param == "cleanup":
                arg = f"--{param}"
                if value == True: x2b_param_list.append(arg)
            # If version is specified, continue
            elif param == "version":
                continue
            # Extract parameters from include / skip lists
            elif param in param_lists:
                arg = extract_params(param, value)
                x2b_param_list.append(arg)
            # Skip positional arguments previously handled
            elif param in positional_args:
                continue
            elif param == "export-only":
                if value == True: xnat_tools_cmd = "xnat-dicom-export"
            # Other arguments follow --param value format.
            else:
                arg = f"--{param} {value}"
                x2b_param_list.append(arg)

        return xnat_tools_cmd, x2b_param_list

def compile_slurm_list(arg_dict, user):
    slurm_param_list = []
    for param, value in arg_dict["slurm-args"].items():
        if value != "" and value is not None:
            arg = f"--{param} {value}"
            slurm_param_list.append(arg)
    return slurm_param_list

def compile_dcm2bids_list(arg_dict, d2b_bindings):
    d2b_param_list = []
    positional_arguments = ["project", "study", "bids_root"]

    if "project" in arg_dict["dcm2bids-args"]:
        project = arg_dict["dcm2bids-args"]["project"]
        d2b_param_list.append(project)
    
    if "subject" in arg_dict["dcm2bids-args"]:
        subject = arg_dict["dcm2bids-args"]["subject"]
        d2b_param_list.append(subject)

    if "bids_root" in arg_dict["dcm2bids-args"]:
        bids_root = arg_dict["dcm2bids-args"]["bids_root"]
        arg = f"{bids_root}"
        d2b_bindings.append(arg)
        d2b_param_list.append(arg)

    for param, value in arg_dict["dcm2bids-args"].items():
        if value == "" or value is  None:
            continue
        if param in positional_arguments:
            continue
        elif param == "overwrite":
            arg = f"--{param}"
            if value == True: d2b_param_list.append(arg) 
        elif param == "cleanup":
            arg = f"--{param}"
            if value == True: d2b_param_list.append(arg)
        elif param == "session-suffix":
            arg = f"--{param} {value}"
            d2b_param_list.append(arg)

    return d2b_param_list

def compile_xnat2bids_list(session, arg_dict, user):
    """Create command line argument list from TOML dictionary."""

    # Create copy of dictionary, so as not to update
    # the original object reference while merging configs.
    arg_dict_copy = copy.deepcopy(arg_dict) 

    bindings = []
    # Compile list of appended arguments
    x2b_param_dict = {}
    for section_name, section_dict in arg_dict_copy.items():
        # Extract xnat2bids-args from original dictionary
        if section_name == "xnat2bids-args":
            x2b_param_dict = section_dict

        # If a session key exist for the current session being 
        # processed, update final config with session block. 
        elif section_name == session:
                x2b_param_dict.update(section_dict)
    
    # Transform session config dictionary into a parameter list.
    xnat_tools_cmd, x2b_param_list = parse_x2b_params(x2b_param_dict, session, bindings)
    return xnat_tools_cmd, x2b_param_list, bindings

async def main():
    # Instantiate argument parserß
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="path to user config")
    args = parser.parse_args()

    # Load default config file into dictionary
    script_dir = pathlib.Path(__file__).parent.resolve()
    default_params = load(f'{script_dir}/x2b_default_config.toml')

    # Set arg_dict. If user provides config, merge dictionaries.
    if args.config is None:
        arg_dict = default_params
    else:
        # Load user configuration
        user_params = load(args.config)

        # Merge with default configuration
        arg_dict = merge_config_files(user_params, default_params)

    # If sessions does not exist in arg_dict, prompt user for Accession ID(s).
    if 'sessions' not in arg_dict['xnat2bids-args'] and "dcm2bids-args" not in arg_dict:
        docs = "https://docs.ccv.brown.edu/bnc-user-manual/xnat-to-bids-intro/using-oscar/oscar-utility-script"
        logging.warning("No sessions were provided in the configuration file. Please specify session(s) to process.")
        logging.info("For helpful guidance, check out our docs at %s", docs)
        sessions_input = input("Enter Session(s) (comma-separated): ")
        arg_dict['xnat2bids-args']['sessions'] = [s.strip() for s in sessions_input.split(',')]
        

    # Fetch user credentials 
    user = input('Enter XNAT Username: ')
    password = getpass('Enter Password: ')

    # Assemble parameter lists per session
    argument_lists = []

    # Initialize bids_root for non-local use
    bids_root = f"/users/{user}/bids-export/"

    # Initialize version and singularity image for non-local use
    version = fetch_latest_version()
    simg=f"/gpfs/data/bnc/simgs/brownbnc/xnat-tools-{version}.sif"

    # Compile parameter list per session for calls to xnat2bids
    if "sessions" in arg_dict['xnat2bids-args']:
        for session in arg_dict['xnat2bids-args']['sessions']:

            # Compile list of slurm parameters.
            slurm_param_list = compile_slurm_list(arg_dict, user)

            # Fetch compiled xnat2bids and slurm parameter lists
            xnat_tools_cmd, x2b_param_list, bindings = compile_xnat2bids_list(session, arg_dict, user)

            # Insert username and password into x2b_param_list
            x2b_param_list.insert(2, f"--user {user}")
            x2b_param_list.insert(3, f"--pass {password}")

            # Fetch latest version if not provided
            if'version' in arg_dict['xnat2bids-args']:
                version = arg_dict['xnat2bids-args']['version']
                simg=f"/gpfs/data/bnc/simgs/brownbnc/xnat-tools-{version}.sif"

            # Define output for logs
            if not ('output' in arg_dict['slurm-args']):
                output = f"/gpfs/scratch/{user}/logs/%x-{session}-%J.txt"
                arg = f"--output {output}"
                slurm_param_list.append(arg)
            else:
                output = arg_dict['slurm-args']['output']

            if not (os.path.exists(os.path.dirname(output))):
                os.makedirs(os.path.dirname(output))

            add_job_name(slurm_param_list, "xnat2bids")

            # Define bids root directory
            if 'bids_root' in arg_dict['xnat2bids-args']:
                bids_root = x2b_param_list[1]
            
            x2b_param_list.insert(1, bids_root)
            bindings.append(bids_root)

            if not (os.path.exists(bids_root)):
                os.makedirs(bids_root)  

            # Store xnat2bids, slurm, and binding paramters as tuple.
            argument_lists.append((xnat_tools_cmd, x2b_param_list, slurm_param_list, bindings))

            # Set logging level per session verbosity. 
            set_logging_level(x2b_param_list)

            # Remove the password parameter from the x2b_param_list
            x2b_param_list_without_password = [param for param in x2b_param_list if not param.startswith('--pass')]

            logging.debug({
            "message": "Argument List",
            "session": session,
                "slurm_param_list": slurm_param_list,
            "x2b_param_list": x2b_param_list_without_password,

            })

    # Run Heudiconv for any user defined sessions
    if "dcm2bids-args" in arg_dict:
        dcm2bids_bindings = []

        # Fetch dcm2bids parameters
        d2b_param_list = compile_dcm2bids_list(arg_dict, dcm2bids_bindings)

        heudiconv_slurm_params = list(slurm_param_list)

        # Define output for logs
        if not ('output' in arg_dict['slurm-args']):
            output = f"/gpfs/scratch/{user}/logs/%x-%J.txt"
            arg = f"--output {output}"
            heudiconv_slurm_params.append(arg)

        add_job_name(heudiconv_slurm_params, "xnat-heudiconv")

        # Add BIDS_ROOT to bindings for heudiconv
        if not ("bids_root" in arg_dict["dcm2bids-args"]):
            d2b_param_list.insert(2, bids_root)
            dcm2bids_bindings.append(bids_root)

        # Add heudiconv command arguments to list for execution
        argument_lists.append(("xnat-heudiconv", d2b_param_list, heudiconv_slurm_params, dcm2bids_bindings))

    # Open output file to field stdout from slurm
    f = open("x2b_launched_jobs.txt", "w+")

    # Loop over argument lists for provided sessions.
    tasks = []
    needs_dependency = False
    for args in argument_lists:
        # Compilie slurm and xnat2bids args 
        xnat_tools_cmd = args[0]
        xnat2bids_param_list = args[1]
        slurm_param_list = args[2]
        bindings_paths = args[3]

        xnat2bids_options = ' '.join(xnat2bids_param_list)
        slurm_options = ' '.join(slurm_param_list)

        # Compile bindings into formated string
        bindings = ' '.join(f"-B {path}" for path in bindings_paths)

        # Build shell script for sbatch
        sbatch_script = f"\"$(cat << EOF #!/bin/sh\n \
            apptainer exec --no-home {bindings} {simg} \
            {xnat_tools_cmd} {xnat2bids_options}\nEOF\n)\""

        # Process command string for SRUN
        sbatch_cmd = shlex.split(f"sbatch {slurm_options} \
            --wrap {sbatch_script}")    

        # Set logging level per session verbosity. 
        set_logging_level(xnat2bids_param_list)

        # Remove the password from sbatch command before logging 
        xnat2bids_options_without_password = []
        exclude_next_opt = False

        for opt in xnat2bids_options.split():
            if exclude_next_opt:
                exclude_next_opt = False
            elif opt == "--pass":
                exclude_next_opt = True
                continue
            else:
                xnat2bids_options_without_password.append(opt)

        sbatch_script_without_password = f"apptainer exec --no-home {bindings} {simg} \
                                            xnat2bids {xnat2bids_options_without_password}"

        sbatch_cmd_without_password = shlex.split(f"sbatch {slurm_options} \
                                                    --wrap {sbatch_script_without_password}")   

        logging.debug({
            "message": "Executing xnat2bids",
            "session": xnat2bids_param_list[0],
            "command": sbatch_cmd_without_password
        })
        
        # Run xnat2bids asynchronously.
        task = asyncio.create_task(asyncio.create_subprocess_exec(*sbatch_cmd, stdout=f))
        tasks.append(task)

    # Wait for stdout to be flushed to output file 
    await asyncio.sleep(1)

    # Establish connection 
    connection = requests.Session()
    connection.verify = True
    connection.auth = (user, password)

    # Fetch pi and study prefixes for BIDS path
    host = arg_dict["xnat2bids-args"]["host"]
    proj, subj = get_project_subject_session(connection, host, session)
    pi_prefix, study_prefix = prepare_path_prefixes(proj, subj)

    # Define bids-validator singularity image path
    simg=f"/gpfs/data/bnc/simgs/bids/validator-latest.sif"

    # Define bids_experiment_dir
    bids_experiment_dir = f"{bids_root}/{pi_prefix}/{study_prefix}/bids"

    # Build shell script for sbatch
    sbatch_bids_val_script = f"\"$(cat << EOF #!/bin/sh\n \
        apptainer exec --no-home -B {bids_experiment_dir} {simg} \
        bids-validator {bids_experiment_dir}\nEOF\n)\""

    # Compile list of slurm parameters.
    bids_val_slurm_params = compile_slurm_list(arg_dict, user)
    output = f"/gpfs/scratch/{user}/logs/%x-%J.txt"
    arg = f"--output {output}"
    bids_val_slurm_params.append(arg)
    slurm_options = ' '.join(bids_val_slurm_params)

    # Process command string for SRUN
    slurm_options = slurm_options.replace("--job-name xnat2bids", "--job-name bids-validator")

    # Fetch JOB-IDs of xnat2bids jobs to wait upon
    afterok_ids = ":".join(fetch_job_ids())

    sbatch_bids_val_cmd = shlex.split(f"sbatch -Q -d afterok:{afterok_ids} {slurm_options} \
        --wrap {sbatch_bids_val_script}") 

    # Run xnat2bids asynchronously.
    task = asyncio.create_task(asyncio.create_subprocess_exec(*sbatch_bids_val_cmd))
    tasks.append(task)

    # Wait for all subprocess tasks to complete
    await asyncio.gather(*tasks)

    # Close and remove output file    
    f.close()
    os.remove(f.name)


    logging.info("Launched %d %s", len(tasks), "jobs" if len(tasks) > 1 else "job")
    logging.info("Processed Scans Located At: %s", bids_root)

if __name__ == "__main__":
    asyncio.run(main())