"""Synthetic public-layout patterns; no private paper/PDF fixture is shipped."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fitz
from app.pdf_structure import inventory,check_coverage

class IEEELayout(unittest.TestCase):
 def document(self):
  d=fitz.open();p=d.new_page()
  p.insert_textbox(fitz.Rect(30,70,560,210),'A regular paragraph with ordinary words. '*24,fontsize=10,fontname='tiro')
  p.insert_text((30,230),'I. INTRODUCTION',fontsize=10,fontname='tiro')
  p.insert_text((30,260),'II. METHOD',fontsize=10,fontname='tiro')
  p.insert_text((30,290),'III. RESULTS AND DISCUSSION',fontsize=10,fontname='tiro')
  p.insert_textbox(fitz.Rect(30,330,560,410),'Fig. 1. Overview of the device.\nFig. 2. Two methods. (a) First method. (b) Second method.',fontsize=9,fontname='tiro')
  p.insert_textbox(fitz.Rect(30,440,560,500),'Fig. 3. Signals. (a) Calculation. (b)(c) Comparison of signals.',fontsize=9,fontname='tiro')
  return d
 def test_roman_headings_in_body_font_are_recognized(self):
  with self.document() as d:r=inventory(d)
  self.assertEqual([s['heading'] for s in r['sections']],['I. INTRODUCTION','II. METHOD','III. RESULTS AND DISCUSSION'])
 def test_two_captions_in_one_block_are_both_detected(self):
  with self.document() as d:r=inventory(d)
  self.assertEqual([f['number'] for f in r['figures']],[1,2,3])
  self.assertNotIn('Fig. 2',r['figures'][0]['caption'])
 def test_parenthesized_and_joined_panel_labels(self):
  with self.document() as d:r=inventory(d)
  self.assertEqual(r['figures'][0]['panels'],[])
  self.assertEqual(r['figures'][1]['panels'],['a','b'])
  self.assertEqual(r['figures'][2]['panels'],['a','b','c'])
 def test_missing_second_figure_still_fails_coverage(self):
  with self.document() as d:expected=inventory(d)
  data={'sections':[{'heading':s['heading']} for s in expected['sections']], 'figures':[]}
  with self.assertRaisesRegex(ValueError,'figure coverage'):check_coverage(data,expected)

if __name__=='__main__':unittest.main()
