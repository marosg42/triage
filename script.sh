while uuids=$(./triage_status_for_agent.py); do
  processed_any=0
  for i in $uuids; do
    echo "Found $i"
    if [ ! -e outputs/${i}-analysis.md ]; then
      echo "$(date) Processing ${i}" | tee -a processed.txt
      sudo rm -rf files/*
      timeout -k 1m 15m opencode run "analyze workflow ${i}" --model google/gemini-3.5-flash
      processed_any=1
    fi
  done
  if [ "$processed_any" -eq 0 ]; then
    echo "All runs already processed."
    break
  fi
done
echo "No more runs to triage."
