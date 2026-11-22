import json,zipfile
import pytest
from pathlib import Path

@pytest.mark.skipif(not Path("/pack.zip").is_file(), reason="Local private original PDFs unavailable")
def test_original_pair(client, monkeypatch):
    def fake(self, *, system_prompt, user_prompt):
        return {"links": [], "atoms": [], "search_more": False,
                "covered": False, "object_consistent": False, "evidence_ids": [],
                "conflicts": [], "uncertain_ids": []}
    monkeypatch.setattr("app.ai.deepseek.DeepSeekClient.complete_json",fake)
    project=client.post("/api/v1/projects",json={"name":"Local original PDF regression"}).json()["id"]
    docs={}
    with zipfile.ZipFile("/pack.zip") as archive:
        for kind in ("tu","znp"):
            name="koktal_"+kind+".pdf"
            response=client.post(f"/api/v1/projects/{project}/documents",data={"kind":kind},
                files={"file":(name,archive.read(name),"application/pdf")})
            assert response.status_code==201,response.text
            docs[kind]=response.json()["id"]
    root=f"/api/v1/projects/{project}/comparisons"
    pair=client.post(root,json={"pairs":[{"tu_document_id":docs["tu"],"znp_document_id":docs["znp"]}]}).json()[0]["id"]
    seen=[]
    from app.requirements.znp_first import ZnPFirstAgent
    original=ZnPFirstAgent.verify_all
    def capture(self,tu,znp,*args,**kwargs):
        rows={a.clause_number:a for a in znp}
        assert "8,6" not in rows["7"].text
        assert "8,6" in rows["9"].text and "0.8" in rows["9"].text
        assert "баллов" in rows["10"].text and "8,6" not in rows["10"].text
        seen.append((len(tu),len(znp)))
        return original(self,tu,znp,*args,**kwargs)
    monkeypatch.setattr(ZnPFirstAgent,"verify_all",capture)
    run=client.post(f"{root}/{pair}/runs?allow_incomplete_sources=true")
    assert run.status_code==201,run.text
    legacy=client.post("/api/v1/requirements/analyze",json={"project_id":project,"allow_incomplete_sources":True})
    assert legacy.status_code==200,legacy.text
    for result in (run.json()["result"],legacy.json()):
        assert result["stats"].get("fulfilled",0)==0
        assert not result["trace_info"]["source_coverage"]["complete"]
    assert len(seen)==2
    print("ORIGINAL_PAIR_API_COUNTS",seen)
