#!/usr/bin/env python
import os
import sys
import argparse
from dotenv import load_dotenv
from subprocess import PIPE, STDOUT, Popen
from simple_slurm import Slurm
import datetime

dir = os.getcwd()

dir_path = os.path.dirname(os.path.realpath(__file__))
#print(dir)
#print(dir_path)

load_dotenv()

XNAT_USER = os.getenv('XNAT_USER')
XNAT_PASSWORD = os.getenv('XNAT_PASSWORD')


# version of xnat2bids being used
version="v1.0.5"
# Path to Singularity Image for xnat-tools (maintained by bnc)
#simg="/gpfs/data/bnc/simgs/brownbnc/xnat-tools-"+version+".sif"
simg=f"/gpfs/data/bnc/simgs/brownbnc/xnat-tools-{version}.sif"
#print(simg)

#--------- directories ---------

data_dir="/gpfs/data/bnc"

# Output directory
#output_dir=data_dir+"/shared/bids-export"
output_dir=f"{data_dir}/shared/bids-export"
#print(output_dir)


if os.path.exists(output_dir):
  print ("Output directory already exists")  
else:
  os.mkdir(output_dir)



# The bidsmap file for your study lives under ${bidsmap_dir}/${bidsmap_file}
#bidsmap_dir='/users/'+ XNAT_USER + '/src/bnc/bnc_demo_dataset/preprocessing/xnat2bids'
bidsmap_dir=f'/users/{XNAT_USER}/src/bnc/bnc_demo_dataset/preprocessing/xnat2bids'

bidsmap_file=f'bidsmap.json'
#tmp_dir='/gpfs/scratch/'+ XNAT_USER +'/xnat2bids'
tmp_dir=f'/gpfs/scratch/{XNAT_USER}/xnat2bids'


if os.path.exists(tmp_dir):
  print ("Temp directory already exists")
else:
  os.mkdir(tmp_dir)


#if not os.path.exists(tmp_dir):
#  os.mkdir(tmp_dir)


#----------- Dictionaries for subject specific variables -----
# Dictionary of sessions to subject


sessions = {"111":"XNAT_E00008", "002":"XNAT13_E00011"}

# Dictionary of series to skip per subject

skip_map = {"111":"-s 1 -s 2 -s 3 -s 4 -s 5 -s 11", "002":"-s 6"}


# Use the task array ID to get the right value for this job
# parameter for sbatch
#SLURM_ARRAY_TASK_ID = "111"

XNAT_SESSION=sessions[Slurm.SLURM_ARRAY_TASK_ID]
SKIP_STRING=skip_map[Slurm.SLURM_ARRAY_TASK_ID]


print(output_dir)
print(bidsmap_dir)
print(simg)
print(XNAT_SESSION)
print(XNAT_USER)
print(XNAT_PASSWORD)
print(bidsmap_file)
print(SKIP_STRING)

#print ("Processing session: ")
#print (XNAT_SESSION) 
#print ("Series to skip:") 
#print (SKIP_STRING) 


#--------- Run xnat2bids ---------

slurm = Slurm(
    array=range(111, 112),
    #array = [2,111],
    cpus_per_task=2,
    mem=16000,
    nodes=1,
    job_name='xnat2bids',
    #/gpfs/scratch/%u/logs/xnat2bids-%A_%a.out
    output=f'/gpfs/scratch/{Slurm.USER_NAME}/logs/xnat2bids-{Slurm.JOB_ARRAY_MASTER_ID}_{Slurm.JOB_ARRAY_ID}.out',
    
    time=datetime.timedelta(days=0,hours=4,minutes=0,seconds=0),
    
 )

# singularity
slurm.sbatch('singularity exec ${simg} xnat2bids --help' + Slurm.SLURM_ARRAY_TASK_ID)

