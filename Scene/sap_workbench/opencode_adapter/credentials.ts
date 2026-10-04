/** Inherit native provider connections without importing any conversation data. */
import {Database} from "bun:sqlite"
import {readFile, writeFile, chmod} from "node:fs/promises"
import {resolve} from "node:path"

type Stamp = {id:string,time_updated:number}
type Row = Stamp & {integration_id:string,label:string,value:string,time_created:number}

export async function inheritCredentials(source:string, target:string) {
  if(source === ":memory:" || resolve(source) === resolve(target) || !await Bun.file(source).exists()) return
  const origin = new Database(source,{readonly:true})
  let destination:Database | undefined
  try {
    if(!origin.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential'").get()) return
    const columns = origin.query("PRAGMA table_info(credential)").all() as {name:string}[]
    if(!columns.some(column=>column.name==="integration_id")) return
    const rows = origin.query("SELECT id,integration_id,label,value,time_created,time_updated FROM credential WHERE integration_id IS NOT NULL").all() as Row[]
    destination = new Database(target)
    const ledgerPath = target+".native-credentials.json"
    let previous:Record<string,Stamp> = {}
    try {previous=JSON.parse(await readFile(ledgerPath,"utf8"))} catch(error) {
      if((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
    }
    const next:Record<string,Stamp> = {}
    const current = destination.query("SELECT id,time_updated FROM credential WHERE integration_id=?")
    const remove = destination.query("DELETE FROM credential WHERE integration_id=?")
    const insert = destination.query("INSERT INTO credential (id,integration_id,label,value,time_created,time_updated) VALUES (?,?,?,?,?,?)")
    destination.transaction(()=>{
      for(const [integration,stamp] of Object.entries(previous)) {
        if(rows.some(row=>row.integration_id===integration)) continue
        const local = current.get(integration) as Stamp | null
        if(local?.id===stamp.id && local.time_updated===stamp.time_updated) remove.run(integration)
      }
      for(const row of rows) {
        const local = current.get(row.integration_id) as Stamp | null, old = previous[row.integration_id]
        // A connection edited inside this private native instance remains its
        // own setting. Refresh only absent or previously inherited records.
        if(!local || (old && local.id===old.id && local.time_updated===old.time_updated)) {
          remove.run(row.integration_id)
          insert.run(row.id,row.integration_id,row.label,row.value,row.time_created,row.time_updated)
          next[row.integration_id]={id:row.id,time_updated:row.time_updated}
        } else if(local.id===row.id && local.time_updated===row.time_updated) {
          next[row.integration_id]={id:row.id,time_updated:row.time_updated}
        }
      }
    })()
    await writeFile(ledgerPath,JSON.stringify(next),{mode:0o600})
    await chmod(ledgerPath,0o600)
    await chmod(target,0o600)
  } finally {destination?.close();origin.close()}
}
