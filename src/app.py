"""Lambda entry point. Keep CMD ["app.lambda_handler"] (image contract)."""
import traceback

from config import from_env
from handler import handle_event


def lambda_handler(event, context):
    try:
        cfg = from_env()
        print("INPUT_BUCKET =%s" % cfg.input_bucket)
        print("OUTPUT_BUCKET=%s" % cfg.output_bucket)
        outcome = handle_event(event, cfg)
        ok = sum(1 for r in outcome["results"] if r["status"] == "ok")
        total = len(outcome["results"])
        return {
            "statusCode": 200,
            "message": "Processed %d/%d record(s) successfully." % (ok, total),
            "results": outcome["results"],
        }
    except Exception as exc:
        traceback.print_exc()
        return {"statusCode": 500, "message": str(exc)}
