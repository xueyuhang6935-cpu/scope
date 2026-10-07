import dotenv from 'dotenv';
import pg from 'pg';
import { readFileSync, readdirSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createObjectStorage, hydrate } from '../server/object-storage.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
dotenv.config({path:path.join(root,'.env'),quiet:true});
const config=JSON.parse(readFileSync(path.join(root,'data/services/local-storage.json'),'utf8'));
const backup=process.argv[2] || path.join(root,'data/backups',readdirSync(path.join(root,'data/backups')).filter(n=>n.startsWith('postgres-s3-')).sort().at(-1));
const manifest=JSON.parse(readFileSync(path.join(backup,'manifest.json'),'utf8'));
const name='scope_restore_verify_'+randomUUID().replaceAll('-','');
const admin=new pg.Client({host:'127.0.0.1',port:55432,user:'postgres',password:config.pgAdmin,database:'postgres'});await admin.connect();
await admin.query(`CREATE DATABASE ${name} OWNER scope_app`);
const url=new URL(process.env.ANALYST_DATABASE_URL);url.pathname='/'+name;
let restored;
const objects=createObjectStorage();
try {
  const child=spawn(process.execPath,['scripts/restore-storage.mjs',backup],{cwd:root,windowsHide:true,stdio:'inherit',env:{...process.env,ANALYST_DATABASE_URL:url.toString()}});
  const [code]=await once(child,'exit');assert.equal(code,0);
  restored=new pg.Client({connectionString:url.toString()});await restored.connect();
  const refs=(await restored.query('SELECT DISTINCT object_key FROM analyst_artifacts')).rows;
  assert.equal(refs.length,manifest.objects.length);
  const tasks=(await restored.query('SELECT response_json FROM analyst_tasks')).rows;
  for(const task of tasks) {
    const response=await hydrate(objects,task.response_json);
    if(response.report_zip_base64)assert.ok(Buffer.from(response.report_zip_base64,'base64').subarray(0,2).equals(Buffer.from('PK')));
    assert.equal(JSON.stringify(response).includes('"_artifact":true'),false);
  }
  console.log(`PASS: backup restored into an isolated database; ${tasks.length} historical responses and ${refs.length} object references verified.`);
} finally {
  if(restored)await restored.end();objects.close();
  await admin.query(`DROP DATABASE ${name}`);await admin.end();
}
