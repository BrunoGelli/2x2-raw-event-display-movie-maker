#!/usr/bin/env bash
# Supply your authorized account/QOS/constraint via sbatch arguments.
# Example: sbatch -A YOUR_ACCOUNT -C cpu -q regular examples/render.sbatch \
#              /absolute/packet.h5 /absolute/movies
#SBATCH --job-name=raw-movie
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --time=00:30:00
#SBATCH --output=raw-movie-%j.log
set -euo pipefail
: "${RAW_MOVIE_REPO:?Set the absolute checkout path}"
: "${RAW_MOVIE_WORK_DIR:?Set scratch location for temporary hit runs}"
input=${1:?Pass input file or quoted glob}
output=${2:?Pass output directory}
source "$RAW_MOVIE_REPO/.venv/bin/activate"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
srun raw-movie render "$input" \
    --geometry-dir "$RAW_MOVIE_REPO/layout" \
    --output-dir "$output" --work-dir "$RAW_MOVIE_WORK_DIR" \
    --threads "${SLURM_CPUS_PER_TASK:-4}" --skip-existing
