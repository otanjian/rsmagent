import {expect,test} from "bun:test"
import {Database} from "bun:sqlite"
import {mkdtemp,rm,stat} from "node:fs/promises"
import {join} from "node:path"
import {tmpdir} from "node:os"
import {inheritCredentials} from "./credentials"

test("native connection inheritance refreshes and removes inherited credentials while preserving private settings and history",async()=>{
  const folder=await mkdtemp(join(tmpdir(),"sap-credential-inherit-"))
  const source=join(folder,"original.db"),target=join(folder,"private.db")
  const original=new Database(source),local=new Database(target)
  try {
    for(const db of [original,local]) db.run("CREATE TABLE credential (id TEXT PRIMARY KEY,integration_id TEXT,label TEXT,value TEXT,time_created INTEGER,time_updated INTEGER)")
    original.run("CREATE TABLE session (id TEXT)");original.run("INSERT INTO session VALUES ('original-only')")
    local.run("CREATE TABLE session (id TEXT)");local.run("INSERT INTO session VALUES ('private-only')")
    const put=original.query("INSERT INTO credential VALUES (?,?,?,?,?,?)")
    put.run("cred_a","deepseek","fixture",JSON.stringify({type:"key",key:"fake-first"}),1,1)
    await inheritCredentials(source,target)
    expect(local.query("SELECT id,time_updated FROM credential").get()).toEqual({id:"cred_a",time_updated:1})
    expect(local.query("SELECT id FROM session").all()).toEqual([{id:"private-only"}])
    expect(original.query("SELECT id FROM session").all()).toEqual([{id:"original-only"}])
    expect((await stat(target)).mode&0o777).toBe(0o600)
    expect((await stat(target+".native-credentials.json")).mode&0o777).toBe(0o600)
    original.query("UPDATE credential SET time_updated=?,value=? WHERE id=?").run(2,JSON.stringify({type:"key",key:"fake-refreshed"}),"cred_a")
    await inheritCredentials(source,target)
    expect(local.query("SELECT time_updated FROM credential").get()).toEqual({time_updated:2})
    local.query("UPDATE credential SET time_updated=?,value=? WHERE id=?").run(3,JSON.stringify({type:"key",key:"fake-local"}),"cred_a")
    await inheritCredentials(source,target)
    expect(local.query("SELECT time_updated FROM credential").get()).toEqual({time_updated:3})
    put.run("cred_b","another","fixture",JSON.stringify({type:"key",key:"fake-another"}),4,4)
    await inheritCredentials(source,target)
    original.run("DELETE FROM credential")
    await inheritCredentials(source,target)
    expect(local.query("SELECT id,time_updated FROM credential").all()).toEqual([{id:"cred_a",time_updated:3}])
    expect(original.query("SELECT COUNT(*) AS count FROM credential").get()).toEqual({count:0})
  } finally {original.close();local.close();await rm(folder,{recursive:true,force:true})}
})
