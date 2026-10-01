import argparse
import asyncio
import copy
from collections import defaultdict
import datetime
import difflib
from enum import Enum
from getpass import getpass
import glob
import logging
import os
import pathlib
import requests
import shlex
import time
import re
from toml import load

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.NOTSET)
logging.basicConfig(level=logging.INFO)
logging.getLogger('asyncio').setLevel(logging.WARNING)

# Disable DEBUG logging for urllib3
urllib3_logger = logging.getLogger('urllib3')
urllib3_logger.setLevel(logging.WARNING)

# PARAM_VAL (--param val)
# MULTI_VAL (-param 1, --param 2, --param n)
# FLAG_ONLU (--param)
class ParamType(Enum):
    PARAM_VAL = 0
    MULTI_VAL = 1
    FLAG_ONLY = 2
    MULTI_FLAG = 3

# param_name: (param_type, needs_binding)
xnat2bids_params = {
    "bidsmap-file": (ParamType.PARAM_VAL, True),
    "bids_root": (ParamType.PARAM_VAL, True),
    "cleanup": (ParamType.FLAG_ONLY, False),
    "dicomfix-config":(ParamType.PARAM_VAL, True),
    "export-only": (ParamType.FLAG_ONLY, False),
    "host": (ParamType.PARAM_VAL, False),
    "includeseq": (ParamType.MULTI_VAL, False),
    "log-id": (ParamType.PARAM_VAL, False),
    "overwrite": (ParamType.FLAG_ONLY, False),
    "sessions": (ParamType.MULTI_VAL, False),
    "skip-export": (ParamType.FLAG_ONLY, False),
    "skipseq": (ParamType.MULTI_VAL, False),
    "validate_frames": (ParamType.FLAG_ONLY, False),
    "reface-mode": (ParamType.PARAM_VAL, False),
    "version": (ParamType.PARAM_VAL, False),
    "verbose": (ParamType.MULTI_FLAG, False),
}

config_params = {
    "project": (ParamType.PARAM_VAL, False),
    "subjects": (ParamType.MULTI_VAL, False),
}

def suggest_similar(input_value, valid_options):
    suggestions = difflib.get_close_matches(input_value, valid_options, n=1, cutoff=0.6)
    return suggestions[0] if suggestions else None

def verify_parameters(config):
    config_dict = load(config)
    x2b_params = config_dict['xnat2bids-args']
    for k, v in x2b_params.items():
        if not (k in xnat2bids_params or k in config_params):
            logging.error(f"Invalid parameter in configuration file: {k} ")
            logging.error("Please resolve invalid parameters before running.")
            suggestion = suggest_similar(k, list(xnat2bids_params.keys()) + list(config_params.keys()))
            if suggestion:
                print(f"Did you mean: {suggestion}?")
            exit()

    # detect duplicate subjects or sessions
    for param in ['subjects','sessions']:
        if param in x2b_params.keys():
            duplicates = [item for item in set(x2b_params[param]) if x2b_params[param].count(item) > 1]
            if duplicates:
                logging.error(f"Detected duplicate subjects or sessions: {duplicates}")
                logging.error("Please resolve before running.")
                exit()

    # if subjects are specified, project must also be specified
    if 'subjects' in x2b_params.keys():
        if 'project' not in x2b_params.keys():
            logging.error("Subjects listed in configuration file, but project not specified.")
            logging.error('Please add project="PROJECT_NAME" to your configuration file')
            exit()
    
    # allow project+subject or sessions, but not both at the same time
    if 'subjects' in x2b_params.keys() and 'sessions' in x2b_params.keys():
        logging.error("Both subjects and sessions are defined in configuration file.")
        logging.error("Please specify with either (project & subject) OR session (XNAT Accession #)")
        exit()
    elif 'project' in x2b_params.keys() and 'sessions' in x2b_params.keys():
        logging.error("Both project and sessions are defined in configuration file.")
        logging.error("Please specify with either (project & subject) OR session (XNAT Accession #)")
        exit()

def get_user_credentials():
    user = input('Enter XNAT Username: ')
    password = getpass('Enter Password: ')
    return user, password

def merge_default_params(config_path, default_params):
    if config_path is None:
        return default_params
    user_params = load(config_path)
    return merge_config_files(user_params, default_params)

def parse_cli_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('bids_root', nargs='?', default=None)
    parser.add_argument('--diff', action=argparse.BooleanOptionalAction, help="diff report between bids_root and remote XNAT")
    parser.add_argument('--update', action=argparse.BooleanOptionalAction, help="diff report between bids_root and remote XNAT")
    parser.add_argument("--config", help="path to user config")

    args = parser.parse_args()
    return args  

def prompt_user_for_sessions(arg_dict):
    docs = "https://docs.ccv.brown.edu/bnc-user-manual/xnat-to-bids-intro/using-oscar/oscar-utility-script"
    logging.warning("No sessions were provided in the configuration file. Please specify session(s) to process.")
    logging.info("For helpful guidance, check out our docs at %s", docs)
    sessions_input = input("Enter Session(s) (comma-separated): ")
    arg_dict['xnat2bids-args']['sessions'] = [s.strip() for s in sessions_input.split(',')]

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

def get_sessions_from_project_subjects(connection, host, project, subjects):
    sessions = []
    for subj in subjects:
        r = get(
            connection,
            host + f"/data/projects/{project}/subjects/{subj}/experiments",
            params={"format": "json"},
        )
        projectValues = r.json()["ResultSet"]["Result"]
        sessions.extend(extractSessions(projectValues))

    return sessions

def get_sessions_from_project(connection, host, project):

    r = get(
        connection,
        host + f"/data/projects/{project}/experiments",
        params={"format": "json"},
    )
    
    return r.json()["ResultSet"]["Result"]

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

def fetch_latest_simg(container_name):
    list_of_versions = glob.glob(f'/oscar/data/bnc/simgs/*/*{container_name}*') 

    # Regular expression to extract version numbers from filenames
    version_regex = re.compile(r'(?:v)?(\d+\.\d+\.\d+)\.sif')

    # Dictionary to hold images and their parsed versions
    image_versions = {}

    for image in list_of_versions:
        match = version_regex.search(image)
        if match:
            image_version = match.group(1)
            image_versions[image] = tuple(map(int, image_version.split('.')))

    # Find the image with the highest version number
    most_recent_image = max(image_versions, key=image_versions.get)
    return most_recent_image

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
            arg.append(f"--{param} \"{v}\"")

    return ' '.join(arg)

def extractSessions(results):
    sessions = []
    for experiment in results:
        sessions.append(experiment['ID'])
    return sessions

def fetch_job_ids(stdout):
    jobs = []
    if isinstance(stdout, bytes):
        stdout = [stdout]

    for line in stdout:
        job_id = line.replace(b'Submitted batch job ', b'' ).strip().decode('utf-8')
        jobs.append(job_id)

    return jobs

def get_datetime(date):
    year, month, day = date.split(" ")[0].split("-")
    return datetime.datetime(int(year), int(month), int(day))

def diff_data_directory(bids_root, user, password):

    missing_sessions = []

    # Establish connection 
    connection = requests.Session()
    connection.verify = True
    connection.auth = (user, password)

    host = "https://xnat.bnc.brown.edu"

    # Gather list of existing PIs in data directory
    pis = [proj.name for proj in os.scandir(bids_root) if proj.is_dir()]

    for pi in pis:
        # Gather list of studies for every project
        studies = [stu.name.split("-")[1] for stu in os.scandir(f"{bids_root}/{pi}")]

        for study in studies:
          
            # Request all sessions in PROJECT_STUDY from XNAT.
            pi_study = f"{pi}_{study}".upper()
            sessions = get_sessions_from_project(connection, host, pi_study)
            
            for session in sessions:
                # Get date of most recent change for every session
                latest_date = get_datetime(session['date'])
                date_added = get_datetime(session['insert_date'])
                

                # Sessions with label format SUBJECT_SESSION are one among many, named ses-SESSION
                if "_" in session['label']:
                    subj, sess = session['label'].lower().split("_")
                # Sessions with SUBJECT as label are the only session. Default to ses-01
                else:
                    subj = session['label']
                    sess = "01"

                ses_path = f"{bids_root}/{pi}/study-{study}/bids/sub-{subj}/ses-{sess}"

                # Add to list of sessions to sync if path does not exist. 
                if not (os.path.exists(ses_path)):
                    missing_sessions.append({'pi': pi, 'study': study, 'subject': subj, 'session': sess, 'ID': session['ID']} )
                else:
                    # For existing paths, check for more recent changes via date of last export and XNAT's session date.
                    c_time = os.path.getctime(ses_path)
                    l_time = time.localtime(c_time)
                    data_date = datetime.datetime(l_time.tm_year, l_time.tm_mon, l_time.tm_mday)

                    if (date_added > data_date or latest_date > data_date):
                        missing_sessions.append({'pi': pi, 'study': study, 'subject': subj, 'session': sess, 'ID': session['ID']} )

    connection.close()

    return missing_sessions

def generate_diff_report(sessions_to_update):

    for session_data in sessions_to_update:
        project = session_data['pi']
        study = session_data['study']
        subject = session_data['subject']
        session = session_data['session']
        ID = session_data['ID']

        if session == '':
            logging.info(f"Missing session information for {project}/{study}/{subject} (ID: {ID})")
        else:
            logging.info(f"Session {session} found for {project}/{study}/{subject} (ID: {ID})")


def fetch_requested_sessions(arg_dict, user, password):
    # Initialize sessions list
    sessions = []

    # Establish connection 
    connection = requests.Session()
    connection.verify = True
    connection.auth = (user, password)

    host = arg_dict["xnat2bids-args"]["host"]
        
    if 'project' in arg_dict['xnat2bids-args']:
        project = arg_dict['xnat2bids-args']['project']
    
        if 'subjects' in arg_dict['xnat2bids-args']:
            subjects = arg_dict['xnat2bids-args']['subjects']
            sessions = get_sessions_from_project_subjects(connection, host, project, subjects)

        else:
            sessions = extractSessions(get_sessions_from_project(connection, host, project))

    connection.close()

    if "sessions" in arg_dict['xnat2bids-args']:
        sessions.extend(arg_dict['xnat2bids-args']['sessions'])
    
    return sessions

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
    positional_args = ["sessions", "bids_root"]

    # Handle positional argments SESSION and BIDS_ROOT
    x2b_param_list.append(session)
    
    if "bids_root" in xnat2bids_dict:
        bids_root = xnat2bids_dict["bids_root"]
        arg = f"{bids_root}"
        bindings.append(arg)
        x2b_param_list.append(arg)

    for param, value in xnat2bids_dict.items():
        if not (param in xnat2bids_params or param in config_params):
            logging.error(f"Invalid parameter in configuration file: {param}")
            logging.error("Please resolve invalid parameters before running.")
            suggestion = suggest_similar(k, list(xnat2bids_params.keys()) + list(config_params.keys()))
            if suggestion:
                print(f"Did you mean: {suggestion}?")
            exit()
        if value == "" or value is  None:
            continue
        if param in positional_args or param in config_params:
            continue

        param_type = xnat2bids_params[param][0]
        if param_type == ParamType.PARAM_VAL:
            arg = f"--{param} \"{value}\""
            x2b_param_list.append(arg)
        elif param_type == ParamType.MULTI_VAL:
            arg = extract_params(param, value)
            x2b_param_list.append(arg)
        elif param_type == ParamType.FLAG_ONLY:
            # only add this flag if true in toml
            if value:
                arg = f"--{param}"
                x2b_param_list.append(arg)
        elif param_type == ParamType.MULTI_FLAG:
            arg = f"--{param}"
            for i in range(value):
                x2b_param_list.append(arg)

        needs_binding = xnat2bids_params[param][1]
        if needs_binding:
            bindings.append(value)

    return x2b_param_list

def compile_slurm_list(arg_dict, user):
    slurm_param_list = []
    for param, value in arg_dict["slurm-args"].items():
        if value != "" and value is not None:
            arg = f"--{param} {value}"
            slurm_param_list.append(arg)
    return slurm_param_list


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
    x2b_param_list = parse_x2b_params(x2b_param_dict, session, bindings)
    return x2b_param_list, bindings

def assemble_argument_lists(arg_dict, user, password, bids_root, argument_lists=[]):
    # Compose argument lists for each session 
    for session in arg_dict['xnat2bids-args']['sessions']:
        # Compile list of slurm parameters.
        slurm_param_list = compile_slurm_list(arg_dict, user)

        # Fetch compiled xnat2bids and slurm parameter lists
        x2b_param_list, bindings = compile_xnat2bids_list(session, arg_dict, user)

        # Insert username and password into x2b_param_list
        x2b_param_list.insert(2, f"--user {user}")
        x2b_param_list.insert(3, f"--pass {password}")

        # Define output for logs
        if not ('output' in arg_dict['slurm-args']):
            oscar_user = os.environ["USER"]
            output = f"/oscar/scratch/{oscar_user}/logs/%x-{session}-%J.txt"
            arg = f"--output {output}"
            slurm_param_list.append(arg)
        else:
            output = arg_dict['slurm-args']['output']

        if not (os.path.exists(os.path.dirname(output))):
            os.makedirs(os.path.dirname(output))

        # Define bids root directory
        if 'bids_root' in arg_dict['xnat2bids-args']:
            bids_root = x2b_param_list[1]
        else:
            x2b_param_list.insert(1, bids_root)
            bindings.append(bids_root)

        if not (os.path.exists(bids_root)):
            os.makedirs(bids_root)  

        # Store xnat2bids, slurm, and binding paramters as tuple.
        argument_lists.append((x2b_param_list, slurm_param_list, bindings))

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
    
    return argument_lists, bids_root

async def launch_x2b_jobs(argument_lists, simg, tasks=[], output=[]):
    # Loop over argument lists for provided sessions.
    needs_validation = False
    for args in argument_lists:
        # Compilie slurm and xnat2bids args 
        xnat2bids_param_list = args[0]
        slurm_param_list = args[1]
        bindings_paths = args[2]
            
        xnat2bids_options = ' '.join(xnat2bids_param_list)

        slurm_options = ' '.join(slurm_param_list)


        # Compile bindings into formated string
        bindings = ' '.join(f"-B {path}" for path in bindings_paths)

        # Set needs_validation if --export-only does not exist
        if "--export-only" not in xnat2bids_param_list: needs_validation = True 

        # Build shell script for sbatch
        sbatch_script = f'""apptainer exec --no-home {bindings} {simg} xnat2bids {xnat2bids_options}""'

        # Escape any '$' characters 
        sbatch_escaped_script = sbatch_script.replace('$', '\$')

        # Process command string for SRUN
        sbatch_cmd = ['sbatch'] + shlex.split(slurm_options) + ['--wrap', sbatch_escaped_script]

        # Set logging level per session verbosity. 
        set_logging_level(xnat2bids_param_list)

        # # Remove the password from sbatch command before logging 
        sbatch_script_without_password = re.sub(r'--pass\s+.*?(?=\s--)', '--pass [REDACTED]', sbatch_escaped_script)
        sbatch_cmd_without_password = ['sbatch'] + shlex.split(slurm_options) + ['--wrap', sbatch_script_without_password]

        logging.debug({
            "message": "Executing xnat2bids",
            "session": xnat2bids_param_list[0],
            "command": shlex.join(sbatch_cmd_without_password)
        })
        
        # Run xnat2bids 
        proc = await asyncio.create_subprocess_exec(*sbatch_cmd, stdout=asyncio.subprocess.PIPE)

        stdout, stderr = await proc.communicate()
        output.append(stdout)
    
    return output, needs_validation

async def launch_bids_validator(arg_dict, user, password, bids_root, job_deps):    

    bids_experiments = []
    output = []

    # Establish connection 
    connection = requests.Session()
    connection.verify = True
    connection.auth = (user, password)

    # Fetch pi and study prefixes for BIDS path
    host = arg_dict["xnat2bids-args"]["host"]
    for session in arg_dict["xnat2bids-args"]["sessions"]:
        proj, subj = get_project_subject_session(connection, host, session)
        pi_prefix, study_prefix = prepare_path_prefixes(proj, subj)
        
        # Define bids_experiment_dir
        bids_dir = f"{bids_root}/{pi_prefix}/{study_prefix}/bids"

        if bids_dir not in bids_experiments:
            bids_experiments.append(bids_dir)

    # Close connection
    connection.close()
    
    # Call latest bids-validator from xnat-tools via deno
    simg=fetch_latest_simg('xnat-tools')

    for bids_experiment_dir in bids_experiments:

        # Build shell script for sbatch
        cmd = [
            "apptainer", "exec", "--no-home",
            "-B", f"{bids_experiment_dir}:/bids:ro",
            "-B", f"/oscar/scratch/{os.environ['USER']}:/scratch",
            simg,
            "deno", "run", "-A", "-qr", "jsr:@bids/validator", '/bids',
            ]

        # export DENO_DIR so that deno knows where to cache files in the container
        sbatch_bids_val_script = (
            "export DENO_DIR=/scratch/deno; " + shlex.join(cmd)
        )

        # Compile list of slurm parameters.
        bids_val_slurm_params = compile_slurm_list(arg_dict, user)
        if not ('output' in arg_dict['slurm-args']):
            oscar_user = os.environ["USER"]
            val_output = f"/oscar/scratch/{oscar_user}/logs/%x-%J.txt"
            arg = f"--output {val_output}"
            bids_val_slurm_params.append(arg)
        else:
            x2b_output = arg_dict['slurm-args']['output'].split("/")
            x2b_output[-1] = "%x-%J.txt"
            val_output = "/".join(x2b_output)
            bids_val_slurm_params = [f"--output {val_output}" if "output" in item else item for item in bids_val_slurm_params]

        bids_val_slurm_params.append("--kill-on-invalid-dep=yes")
        slurm_options = ' '.join(bids_val_slurm_params)

        # Process command string for SRUN
        slurm_options = slurm_options.replace('--job-name xnat2bids', '--job-name bids-validator')

        # Fetch JOB-IDs of xnat2bids jobs to wait upon
        afterok_ids = ":".join(job_deps)

        sbatch_bids_val_cmd = ['sbatch'] + ['-d'] + [f'afterok:{afterok_ids}'] + shlex.split(slurm_options) + ['--wrap', sbatch_bids_val_script]

        logging.debug({
            "message": "Executing bids validator",
            "command": shlex.join(sbatch_bids_val_cmd),
        })


        # Run bids-validator
        proc = await asyncio.create_subprocess_exec(*sbatch_bids_val_cmd, stdout=asyncio.subprocess.PIPE)

        stdout, stderr = await proc.communicate()
        output.append(stdout)

    return output,sbatch_bids_val_script

async def main():
    # Instantiate argument parser
    args = parse_cli_arguments()

    # Fetch user credentials 
    user, password = get_user_credentials()

    if (args.config):
        verify_parameters(args.config)

  

    # Load default config file into dictionary
    script_dir = pathlib.Path(__file__).parent.resolve()
    default_params = load(f'{script_dir}/x2b_default_config.toml')

    # Set arg_dict. If user provides config, merge dictionaries.
    arg_dict = merge_default_params(args.config, default_params)

    # Initialize bids_root for non-local use
    bids_root = f"/users/{user}/bids-export/"

    # Initialize version and singularity image for non-local use
    try:
        version = arg_dict['xnat2bids-args']['version']
        simg=f"/oscar/data/bnc/simgs/brownbnc/xnat-tools-{version}.sif"
        # we have to delete the argument so that it doesn't get
        # passed to xnat2bids
        del arg_dict['xnat2bids-args']['version'] 
    except KeyError:
        simg = fetch_latest_simg('xnat-tools')


    if any(key in arg_dict['xnat2bids-args'] for key in ['project', 'subjects', 'sessions']):
        sessions = fetch_requested_sessions(arg_dict, user, password)
        if len(sessions) == 0:
            logging.info("There are no sessions to export. Please check your configuration file for errors.")
            exit()
        else:
            if 'sessions' in arg_dict['xnat2bids-args']:
                for session in sessions:
                    if session not in arg_dict['xnat2bids-args']['sessions']:
                        arg_dict['xnat2bids-args']['sessions'].append(session)
            else:
                arg_dict['xnat2bids-args']['sessions'] = sessions

    if (args.diff):
        data_dir = bids_root
        if args.bids_root:
            data_dir = args.bids_root
        elif "bids_root" in arg_dict['xnat2bids-args']:
            data_dir = arg_dict['xnat2bids-args']["bids_root"]

        sessions_to_update = diff_data_directory(data_dir, user, password)
        generate_diff_report(sessions_to_update)
        return
    
    if args.update:
        data_dir = bids_root
        if args.bids_root:
            data_dir = args.bids_root
        elif "bids_root" in arg_dict['xnat2bids-args']:
            data_dir = arg_dict['xnat2bids-args']["bids_root"]

        sessions_to_update = diff_data_directory(data_dir, user, password)
        session_list = [ses['ID'] for ses in sessions_to_update]
        if len(session_list) == 0:
            logging.info("Your data directory is synced. Exiting.")
            exit()
        else:
            logging.info("Launching jobs for the following sessions:")
            generate_diff_report(sessions_to_update)

            while True:
                confirm = input("Would you like to proceed with the update? (y/n) \n")
                if confirm == "y" or confirm == "Y":
                    break
                elif confirm == "n" or confirm == "N":
                    exit()
                else:
                    logging.info("Your input was not a valid option.")
                    continue


        if 'sessions' in arg_dict['xnat2bids-args']:
            for session in session_list:
                if session not in arg_dict['xnat2bids-args']['sessions']:
                    arg_dict['xnat2bids-args']['sessions'].append(session)
        else:
            arg_dict['xnat2bids-args']['sessions'] = session_list    

    try:
        sessions = arg_dict['xnat2bids-args']['sessions']
    except KeyError:
        sessions = []

    if not sessions:
        prompt_user_for_sessions(arg_dict)

    argument_lists, bids_root = assemble_argument_lists(arg_dict, user, password, bids_root)

    # Launch xnat2bids
    x2b_output, needs_validation = await launch_x2b_jobs(argument_lists, simg)
    x2b_jobs = fetch_job_ids(x2b_output)

    # Launch bids-validator
    if needs_validation:
        validator_output,val_cmd = await launch_bids_validator(arg_dict, user, password, bids_root, x2b_jobs)
        validator_jobs = fetch_job_ids(validator_output)

    # Summary Logging 
    logging.info("Launched %d xnat2bids %s", len(x2b_jobs), "jobs" if len(x2b_jobs) > 1 else "job")
    logging.info("Job %s: %s", "IDs" if len(x2b_jobs) > 1 else "ID", ' '.join(x2b_jobs))

    if needs_validation:
        logging.info("Launched %d bids-validator %s to check BIDS compliance", len(validator_jobs), "jobs" if len(validator_jobs) > 1 else "job")
        logging.info("Job %s: %s", "IDs" if len(validator_jobs) > 1 else "ID", ' '.join(validator_jobs))
        correct_for_val_cmd = f"apptainer exec --no-home -B {bids_root}:/bids {simg} python -c 'from xnat_tools.bids_utils import correct_for_bids_schema_validator; correct_for_bids_schema_validator(\"/bids\")'"
        logging.info(
            "\n\n***********\n"
            "We have recently upgraded to the BIDS validator 2.0.\n"
            "\nThis version checks metadata more thoroughly, so it identifies errors that the "
            "legacy validator (https://bids-standard.github.io/legacy-validator/) did not."
            "\nTo make existing data in this BIDS directory compatible with the new validator, "
            "paste the following into the terminal (on OOD or an interact session):\n\n"
            f"{correct_for_val_cmd}\n\n\n"
            "Then re-run BIDS validation with:\n\n"
            f"{val_cmd}\n\n"
            "Please contact cobre-bnc@brown.edu with any issues!\n"
            "***********\n\n"
        )

    logging.info("Processed Scans Located At: %s", bids_root)

if __name__ == "__main__":
    asyncio.run(main())