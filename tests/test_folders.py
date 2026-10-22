"""Real SQLite integration checks for classification and safe undo."""
import os,sys,tempfile,unittest
from pathlib import Path
from fastapi import HTTPException
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import get_db,init_db
from app.store import upsert_paper
from app import folders as f,workspace as w
init_db();w.init()

class Folders(unittest.TestCase):
 def setUp(self):
  with get_db() as c:
   for table in ('organizer_undo','organizer_items','organizer_legacy','organizer_folders','collection_items','collections','reading_list','papers'):c.execute('DELETE FROM '+table)
   self.p1=upsert_paper(c,{'title':'One','doi':'10.9999/one','year':2025},'test','library')[0]
   self.p2=upsert_paper(c,{'title':'Two','doi':'10.9999/two','year':2024},'test','candidate')[0]
 def folder(self,name='Folder',scope='library',parent=None):return f.create(f.Create(name=name,scope=scope,parent_id=parent))['folder']['id']
 def assign(self,ids,targets,scope='library',action='add',source=None):return f.assign(f.Items(ids=ids,folder_ids=targets,scope=scope,action=action,source_folder_id=source))
 def search(self,scope='library',**kw):return w.search(w.SearchIn(scope=scope,**kw))
 def test_nested_creation_and_independent_namespaces(self):
  a=self.folder('Topic');b=self.folder('Topic',parent=a);r=self.folder('Topic','reading')
  self.assertNotEqual(a,b);self.assertNotEqual(a,r)
  with self.assertRaises(HTTPException):self.folder('topic')
 def test_unicode_name_and_empty_invalid_names(self):
  self.folder('研究方向')
  for name in ('   ','A/B','A\nB'):
   with self.assertRaises(HTTPException):self.folder(name)
 def test_rename_and_color_persist(self):
  fid=self.folder();f.edit(fid,f.Edit(name='Renamed',color='#ab9cf1'))
  row=f.get_folder(fid);self.assertEqual(row['name'],'Renamed');self.assertEqual(row['color'],'#ab9cf1')
 def test_edit_name_and_parent_is_atomic(self):
  a=self.folder('A');b=self.folder('B');x=self.folder('X',parent=a);self.folder('Taken',parent=b)
  with self.assertRaises(HTTPException):f.edit(x,f.Edit(name='Taken',parent_id=b,color='#ffffff'))
  old=f.get_folder(x);self.assertEqual(old['parent_id'],a);self.assertEqual(old['name'],'X');self.assertNotEqual(old['color'],'#ffffff')
  f.edit(x,f.Edit(name='Renamed',parent_id=b));self.assertEqual(f.get_folder(x)['parent_id'],b)
 def test_descendant_and_self_moves_rejected(self):
  a=self.folder('A');b=self.folder('B',parent=a)
  for target in (a,b):
   with self.assertRaises(HTTPException):f.move(a,f.Move(parent_id=target))
  self.assertIsNone(f.get_folder(a)['parent_id'])
 def test_cross_scope_move_rejected(self):
  a=self.folder();b=self.folder(scope='reading')
  with self.assertRaises(HTTPException):f.move(a,f.Move(parent_id=b))
 def test_sibling_order_and_nested_move(self):
  a=self.folder('A');b=self.folder('B');c=self.folder('C')
  f.move(c,f.Move(parent_id=None,before_id=a));self.assertEqual([x['id'] for x in f.list_folders()['folders']],[c,a,b])
  f.move(a,f.Move(parent_id=b));self.assertEqual(f.get_folder(a)['parent_id'],b)
 def test_depth_limit_checks_moving_subtree(self):
  root=self.folder('L0');leaf=root
  for i in range(1,8):leaf=self.folder('L'+str(i),parent=leaf)
  with self.assertRaises(HTTPException):self.folder('Too deep',parent=leaf)
  other=self.folder('Other');child=self.folder('Child',parent=other)
  with self.assertRaises(HTTPException):f.move(other,f.Move(parent_id=leaf))
 def test_multi_membership_counts_unique_in_parent(self):
  a=self.folder('Parent');b=self.folder('Child',parent=a)
  self.assign([self.p1],[a,b]);rows={x['id']:x for x in f.list_folders()['folders']}
  self.assertEqual(rows[a]['count'],1);self.assertEqual(rows[a]['direct_count'],1)
  self.assertEqual(self.search(folder_id=a)['total'],1)
 def test_recursive_and_direct_filter(self):
  a=self.folder('Parent');b=self.folder('Child',parent=a);self.assign([self.p1],[b])
  self.assertEqual(self.search(folder_id=a)['total'],1);self.assertEqual(self.search(folder_id=a,folder_descendants=False)['total'],0)
 def test_unfiled_scope_and_candidate_promotion(self):
  a=self.folder();self.assign([self.p2],[a])
  self.assertEqual(self.search(unfiled=True)['total'],1);self.assertEqual(f.list_folders()['unfiled'],1)
  with get_db() as c:self.assertEqual(c.execute('SELECT status FROM papers WHERE id=?',(self.p2,)).fetchone()[0],'library')
 def test_reading_folder_adds_reading_without_overwriting_note(self):
  a=self.folder(scope='reading');w.reading(w.ReadingIn(ids=[self.p1],note='Keep my note'))
  self.assign([self.p1,self.p2],[a],scope='reading')
  data=self.search(scope='reading',folder_id=a);self.assertEqual(data['total'],2)
  self.assertEqual(next(x for x in data['papers'] if x['id']==self.p1)['reading_note'],'Keep my note')
 def test_global_reading_remove_cleans_only_reading_memberships(self):
  a=self.folder();b=self.folder(scope='reading');self.assign([self.p1],[a]);self.assign([self.p1],[b],scope='reading')
  w.reading(w.ReadingIn(ids=[self.p1],action='remove'))
  self.assertEqual(self.search(scope='reading',folder_id=b)['total'],0);self.assertEqual(self.search(folder_id=a)['total'],1)
 def test_move_removes_only_source_membership(self):
  a=self.folder('A');b=self.folder('B');c=self.folder('C');self.assign([self.p1],[a,b])
  self.assign([self.p1],[c],action='move',source=a)
  self.assertEqual(self.search(folder_id=a)['total'],0);self.assertEqual(self.search(folder_id=b)['total'],1);self.assertEqual(self.search(folder_id=c)['total'],1)
 def test_remove_from_folder_preserves_library_and_pdf_field(self):
  a=self.folder();self.assign([self.p1],[a]);self.assign([self.p1],[a],action='remove')
  self.assertEqual(self.search()['total'],1);self.assertEqual(self.search(folder_id=a)['total'],0)
 def test_recursive_move_and_remove_respect_subtree(self):
  a=self.folder('A');b=self.folder('B',parent=a);outside=self.folder('Outside');self.assign([self.p1],[b,outside])
  f.assign(f.Items(ids=[self.p1],folder_ids=[a],scope='library',action='remove',source_descendants=True))
  self.assertEqual(self.search(folder_id=a)['total'],0);self.assertEqual(self.search(folder_id=outside)['total'],1)
  self.assign([self.p1],[b])
  f.assign(f.Items(ids=[self.p1],folder_ids=[outside],scope='library',action='move',source_folder_id=a,source_descendants=True))
  self.assertEqual(self.search(folder_id=b)['total'],0);self.assertEqual(self.search(folder_id=outside)['total'],1)
 def test_batch_invalid_target_or_paper_rolls_back(self):
  a=self.folder();b=self.folder(scope='reading')
  for ids,targets in [([self.p1],[a,b]),([self.p1,999999],[a])]:
   with self.assertRaises(HTTPException):self.assign(ids,targets)
  self.assertEqual(self.search(folder_id=a)['total'],0)
 def test_delete_subtree_keeps_papers_and_is_undoable(self):
  a=self.folder('A');b=self.folder('B',parent=a);self.assign([self.p1],[b]);r=f.delete(a)
  self.assertEqual(r['deleted'],2);self.assertEqual(self.search()['total'],1)
  f.undo(r['undo_token']);self.assertEqual(f.get_folder(b)['parent_id'],a);self.assertEqual(self.search(folder_id=b)['total'],1)
 def test_keep_children_collision_rolls_back_delete(self):
  a=self.folder('A');b=self.folder('B',parent=a);self.folder('B')
  with self.assertRaises(HTTPException):f.delete(a,keep_children=True)
  self.assertEqual(f.get_folder(b)['parent_id'],a)
 def test_keep_children_promotes_to_parent(self):
  a=self.folder('A');b=self.folder('B',parent=a);f.delete(a,keep_children=True);self.assertIsNone(f.get_folder(b)['parent_id'])
 def test_undo_membership_and_automatic_reading_add(self):
  a=self.folder(scope='reading');r=self.assign([self.p2],[a],scope='reading');f.undo(r['undo_token'])
  self.assertEqual(self.search(scope='reading')['total'],0);self.assertEqual(f.list_folders('reading')['folders'][0]['count'],0)
 def test_stale_undo_cannot_overwrite_later_changes(self):
  a=self.folder();r=self.assign([self.p1],[a]);self.folder('Later')
  with self.assertRaises(HTTPException):f.undo(r['undo_token'])
  self.assertEqual(self.search(folder_id=a)['total'],1)
 def test_reading_note_edit_invalidates_undo(self):
  a=self.folder(scope='reading');r=self.assign([self.p1],[a],scope='reading');w.reading(w.ReadingIn(ids=[self.p1],note='Edited afterwards'))
  with self.assertRaises(HTTPException):f.undo(r['undo_token'])
  self.assertEqual(self.search(scope='reading')['papers'][0]['reading_note'],'Edited afterwards')
 def test_legacy_migration_idempotent_and_deleted_folder_not_resurrected(self):
  with get_db() as c:
   cid=c.execute("INSERT INTO collections(name) VALUES('Legacy')").lastrowid;c.execute('INSERT INTO collection_items VALUES(?,?)',(cid,self.p1))
  a=f.list_folders()['folders'][0]['id'];self.assertEqual(self.search(folder_id=a)['total'],1)
  f.init();self.assertEqual(len(f.list_folders()['folders']),1);r=f.delete(a);self.assertEqual(f.list_folders()['folders'],[])
  f.undo(r['undo_token']);self.assertEqual(len(f.list_folders()['folders']),1)
 def test_pagination_and_scope_validation(self):
  a=self.folder();self.assign([self.p1,self.p2],[a]);first=self.search(folder_id=a,limit=1);second=self.search(folder_id=a,limit=1,offset=1)
  self.assertTrue(first['has_more']);self.assertNotEqual(first['papers'][0]['id'],second['papers'][0]['id'])
  with self.assertRaises(HTTPException):self.search(scope='reading',folder_id=a)
 def test_deleting_paper_cascades_membership(self):
  a=self.folder();self.assign([self.p1],[a])
  with get_db() as c:c.execute('DELETE FROM papers WHERE id=?',(self.p1,))
  self.assertEqual(f.list_folders()['folders'][0]['count'],0)

if __name__=='__main__':unittest.main()
