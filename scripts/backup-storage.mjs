import dotenv from 'dotenv';
import pg from 'pg';
import { mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createObjectStorage } from '../server/object-storage.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
dotenv.config({path:path.join(root,'.env'),quiet:true});
const target=path.resolve(root,process.argv[2] || `data/backups/postgres-s3-${new Date().toISOString().replaceAll(':','-')}`);
await mkdir(path.join(target,'objects'),{recursive:true});
const db=new pg.Client({connectionString:process.env.ANALYST_DATABASE_URL}); await db.connect();
const objects=createObjectStorage();
try {
  // Both pg_dump and artifact manifest use the SAME exported MVCC snapshot.
  await db.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');
  const snapshot=(await db.query('SELECT pg_export_snapshot() AS id')).rows[0].id;
  const refs=(await db.query('SELECT DISTINCT object_key,sha256,content_type,size_bytes FROM analyst_artifacts')).rows;
  const url=new URL(process.env.ANALYST_DATABASE_URL);
  const dump=process.env.ANALYST_PG_DUMP || path.join(process.env.SystemDrive || 'C:',path.sep,'ScopeLocalServices','pgsql','bin','pg_dump.exe');
  const child=spawn(dump,['--host',url.hostname,'--port',url.port || '5432','--username',decodeURIComponent(url.username),
    '--dbname',url.pathname.slice(1),'--format=custom','--no-owner','--no-acl',`--snapshot=${snapshot}`,'--file',path.join(target,'database.dump')],
    {windowsHide:true,stdio:['ignore','ignore','pipe'],env:{...process.env,PGPASSWORD:decodeURIComponent(url.password),PGSSLMODE:url.searchParams.get('sslmode') || 'prefer'}});
  let error='';child.stderr.on('data',b=>{error+=b.toString();});
  const [code]=await once(child,'exit');if(code!==0)throw new Error(`pg_dump failed (${code}): ${error}`);
  await db.query('COMMIT');
  // Application object keys are immutable and have no automatic deletion lifecycle.
  // Copy the exact referenced objects; hash verification detects missing/corrupt data.
  const manifest=[];
  for(const r of refs) {
    const data=await objects.get({key:r.object_key,sha256:r.sha256});
    await writeFile(path.join(target,'objects',r.sha256),data);
    manifest.push({key:r.object_key,sha256:r.sha256,size:Number(r.size_bytes),content_type:r.content_type});
  }
  const {readFile}=await import('node:fs/promises');
  const dumpHash=createHash('sha256').update(await readFile(path.join(target,'database.dump'))).digest('hex');
  await writeFile(path.join(target,'manifest.json'),JSON.stringify({format:1,completed_at:new Date().toISOString(),database_sha256:dumpHash,objects:manifest},null,2));
  console.log(JSON.stringify({status:'backup_complete',directory:target,objects:manifest.length}));
} catch(error) {await db.query('ROLLBACK').catch(()=>{});throw error;}
finally {await db.end();objects.close();}
