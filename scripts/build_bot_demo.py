"""Render original BotFlow scenarios with English presentation controls.

Captured dialogue stays in its original language; product source is read only.
Providers, payments and messages are simulated.
"""
import json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='gl-case-preview-') as directory:
 output=Path(directory)/'index.html'
 subprocess.run([sys.executable,'tests/render_bot_preview.py','--output',str(output)],cwd=root/'backend',check=True,env={**os.environ,'PYTHONPATH':str(root/'backend')})
 html=output.read_text()
 payload=next(line for line in html.splitlines() if line.startswith('const DATA = '))
 html=html.replace(payload,'/* ORIGINAL_DIALOGUE_PAYLOAD */')
 translations=json.loads((root/'demo/shell-translations.json').read_text())
 for a,b in sorted(translations.items(),key=lambda x:-len(x[0])):html=html.replace(a,b)
 html=html.replace('lang="ru"','lang="en"')
 html=html.replace('/* ORIGINAL_DIALOGUE_PAYLOAD */',payload.replace('../../gl_bot/assets/products/','products/'))
 html=html.replace('All webinars, bookings and payment states are synthetic. This demonstration sends no payments, video requests or messages. Product card copy is adapted for this portfolio edition.','Synthetic scenarios, original Russian dialogue. English controls are presentation-only. No payments, messages or video requests are sent.')
 (root/'demo/index.html').write_text(html)
 (root/'demo/products').mkdir(exist_ok=True)
 for image in (root/'backend/gl_bot/assets/products').glob('*.png'):shutil.copy2(image,root/'demo/products'/image.name)
print('Rendered original dialogues; snapshot source files unchanged.')
