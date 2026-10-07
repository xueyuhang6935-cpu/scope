import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { createHash } from 'node:crypto';

// Read historical artifacts for manual backup and restore verification.
export function createObjectStorage(env = process.env) {
  for (const key of ['ANALYST_S3_ENDPOINT','ANALYST_S3_BUCKET','ANALYST_S3_ACCESS_KEY','ANALYST_S3_SECRET_KEY']) {
    if (!env[key]) throw new Error(`Missing ${key}`);
  }
  const bucket = env.ANALYST_S3_BUCKET;
  const client = new S3Client({ endpoint: env.ANALYST_S3_ENDPOINT, region: env.ANALYST_S3_REGION || 'us-east-1',
    forcePathStyle: true, credentials: { accessKeyId: env.ANALYST_S3_ACCESS_KEY, secretAccessKey: env.ANALYST_S3_SECRET_KEY },
    requestChecksumCalculation: 'WHEN_REQUIRED', responseChecksumValidation: 'WHEN_REQUIRED' });
  return {
    async get(ref) {
      const result = await client.send(new GetObjectCommand({ Bucket: bucket, Key: ref.key }));
      const body = Buffer.from(await result.Body.transformToByteArray());
      if (createHash('sha256').update(body).digest('hex') !== ref.sha256) throw new Error('Artifact checksum mismatch');
      return body;
    },
    close() { client.destroy(); },
  };
}

export async function hydrate(objects, stored) {
  async function read(value, name = '') {
    if (value?._artifact === true) {
      const bytes = await objects.get(value);
      if (name === 'report_zip_base64') return bytes.toString('base64');
      return value.content_type === 'application/json' ? read(JSON.parse(bytes.toString('utf8')), name) : bytes.toString('utf8');
    }
    if (Array.isArray(value)) return Promise.all(value.map(v => read(v, name)));
    if (value && typeof value === 'object') return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([k,v]) => [k,await read(v,k)])));
    return value;
  }
  return read(stored);
}
