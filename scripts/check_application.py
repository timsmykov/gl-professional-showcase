"""Run the exported application checks without private VM deployment recipes."""
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]/'backend'
files=['test_flow.py','test_buttons.py','test_api_recordings.py','test_information_screens.py','test_recording_dialogues.py','test_api_repository.py','test_server.py','test_settings.py','test_telegram.py','test_telegram_transport.py','test_poller.py','test_rich_products.py']
api=['test_app.py','test_orders.py','test_checkout.py','test_atol_pay.py','test_fiscal.py','test_fiscal_notifications.py','test_video_edge.py','test_security_boundary.py']
args=[sys.executable,'-m','pytest','-q']+[str(root/'tests'/f) for f in files]+[str(root/'tests/api'/f) for f in api]
raise SystemExit(subprocess.call(args,cwd=root))
