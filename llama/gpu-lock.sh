# Shared GPU admission for the llama lifecycle scripts (#1462). Source it, then call gpu_admit.
#
# Same lock as agent_execution/resources.py GpuLease, the media gpu_guard and the transcriber:
# /tmp/nomad-gpu.lock, flock LOCK_EX|LOCK_NB, never waiting. Contention exits 75 (EX_TEMPFAIL)
# with "GPU busy" on stderr before anything is changed. The lock lives on fd 9 until the script
# exits; launch commands must close it (9>&-) so a long-running server never holds admission.
#
# Env: NOMAD_GPU_LOCK (default /tmp/nomad-gpu.lock), NOMAD_GPU_GUARD_STATE (default the file
# gpu_guard keeps while a restore is unfinished, same as the transcriber's TRANSCRIBE_GUARD_STATE).

gpu_admit() {
  [[ -n ${GPU_ADMITTED:-} ]] && return 0   # already held on fd 9; re-opening would drop and retake it
  local lock=${NOMAD_GPU_LOCK:-/tmp/nomad-gpu.lock}
  local state=${NOMAD_GPU_GUARD_STATE:-$HOME/.local/state/content-media-worker/gpu_guard_state.json}
  local want_state=${1:-}   # "check-restore": also refuse while a gpu_guard restore is pending
  if [[ -L $lock ]]; then
    echo "GPU lock $lock is a symlink; refusing" >&2; exit 1
  fi
  exec 9>>"$lock"
  local owner links mode
  read -r owner links mode < <(stat -L -c '%u %h %a' /proc/self/fd/9)
  if [[ ! -f /proc/self/fd/9 || $owner != "$(id -u)" || $links != 1 ]] || (( 8#$mode & 8#022 )); then
    echo "GPU lock $lock must be a regular, single-link file owned by this user and not group/other writable" >&2
    exit 1
  fi
  if ! flock -n 9; then
    echo "GPU busy: another GPU workload holds admission ($lock)" >&2; exit 75
  fi
  # The path must still be the file we locked (it could have been swapped while we opened it).
  if [[ -L $lock || $(stat -c '%d:%i' "$lock") != "$(stat -L -c '%d:%i' /proc/self/fd/9)" ]]; then
    echo "GPU lock path changed during admission" >&2; exit 1
  fi
  GPU_ADMITTED=1
  if [[ $want_state == check-restore && -e $state ]]; then
    echo "GPU busy: a gpu_guard restore is pending ($state); run 'gpu_guard recover' first" >&2; exit 75
  fi
}
