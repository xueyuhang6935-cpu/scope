import dotenv from 'dotenv';
import pg from 'pg';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
dotenv.config({path:path.join(root,'.env'),quiet:true});
if (!process.argv[2]) throw new Error('Usage: node scripts/restore-storage.mjs <backup directory>; destination DB must be empty');
const source=path.resolve(process.argv[2]);
const manifest=JSON.parse(await readFile(path.join(source,'manifest.json'),'utf8'));
const hash=data=>createHash('sha256').update(data).digest('hex');
if(manifest.format!==1 || hash(await readFile(path.join(source,'database.dump')))!==manifest.database_sha256)throw new Error('Invalid backup manifest/dump');
// Validate all files BEFORE any writes.
for(const ref of manifest.objects) {
  if(!/^[a-f0-9]{64}$/.test(ref.sha256))throw new Error('Invalid artifact hash');
  const bytes=await readFile(path.join(source,'objects',ref.sha256));
  if(bytes.length!==ref.size || hash(bytes)!==ref.sha256)throw new Error('Backup object integrity failed');
}
const db=new pg.Client({connectionString:process.env.ANALYST_DATABASE_URL});await db.connect();
try {
  const tables=await db.query("SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') LIMIT 1");
  if(tables.rowCount)throw new Error('Refusing restore into a non-empty database');
} finally {await db.end();}
const s3=new S3Client({endpoint:process.env.ANALYST_S3_ENDPOINT,region:process.env.ANALYST_S3_REGION || 'us-east-1',forcePathStyle:true,
  credentials:{accessKeyId:process.env.ANALYST_S3_ACCESS_KEY,secretAccessKey:process.env.ANALYST_S3_SECRET_KEY},requestChecksumCalculation:'WHEN_REQUIRED'});
try {
  for(const ref of manifest.objects)await s3.send(new PutObjectCommand({Bucket:process.env.ANALYST_S3_BUCKET,Key:ref.key,
    Body:await readFile(path.join(source,'objects',ref.sha256)),ContentType:ref.content_type}));
} finally {s3.destroy();}
const url=new URL(process.env.ANALYST_DATABASE_URL);
const restore=process.env.ANALYST_PG_RESTORE || path.join(process.env.SystemDrive || 'C:',path.sep,'ScopeLocalServices','pgsql','bin','pg_restore.exe');
const child=spawn(restore,['--host',url.hostname,'--port',url.port || '5432','--username',decodeURIComponent(url.username),
  '--dbname',url.pathname.slice(1),'--single-transaction','--exit-on-error','--no-owner','--no-acl',path.join(source,'database.dump')],
  {windowsHide:true,stdio:['ignore','ignore','pipe'],env:{...process.env,PGPASSWORD:decodeURIComponent(url.password),PGSSLMODE:url.searchParams.get('sslmode') || 'prefer'}});
let error='';child.stderr.on('data',b=>error+=b.toString());
const [code]=await once(child,'exit');if(code!==0)throw new Error(`pg_restore failed (${code}): ${error}`);
console.log(JSON.stringify({status:'restored',objects:manifest.objects.length}));
