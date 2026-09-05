from edgedash.config import load_config
from edgedash import storage
from edgedash.agents.verifier import Verifier

cfg = load_config()

print("Running verifier directly...")
try:
    v = Verifier()
    result = v.run(cfg, cfg.db_path)
    print(f"Status : {result.status}")
    print(f"Notes  : {result.notes}")
except Exception as exc:
    import traceback
    print("EXCEPTION in verifier:")
    traceback.print_exc()
