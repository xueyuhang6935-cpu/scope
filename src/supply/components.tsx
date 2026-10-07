import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Link, useLocation, useOutletContext, type LinkProps } from 'react-router-dom';
import { createPortal } from 'react-dom';
import type { SupplyContext } from './SupplyShell';
import { CheckCircle2, CircleAlert, CircleDot, OctagonAlert } from 'lucide-react';
import { useBusinessAccess } from './BusinessAccess';

export type Tone = 'success' | 'warning' | 'error' | 'info';
export function StatusTag({ tone = 'info', children }: { tone?: Tone; children: ReactNode }) {
  const Icon = { success: CheckCircle2, warning: CircleAlert, error: OctagonAlert, info: CircleDot }[tone];
  return <span className={`sp-status sp-status-${tone}`}><Icon size={13} aria-hidden="true" />{children}</span>;
}
export function Button({ primary, permission, permissionFactory, permissionDomain, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; permission?: string; permissionFactory?: string; permissionDomain?: string }) {
  const { allows } = useBusinessAccess();
  const location = useLocation();
  const factory = permissionFactory || new URLSearchParams(location.search).get('factory') || 'all';
  const denied = !!permission && !allows(permission, factory === 'all' ? ['A', 'B'] : [factory], permissionDomain);
  return <button type="button" className={`sp-button ${primary ? 'sp-primary' : ''} ${className}`} {...props} disabled={props.disabled || denied} title={denied ? '当前角色或范围没有此操作权限' : props.title} />;
}
export function ScopedLink({ to, className = '', ...props }: Omit<LinkProps, 'to'> & { to: string }) {
  const location = useLocation();
  const url = new URL(to, window.location.origin);
  const factory = new URLSearchParams(location.search).get('factory');
  if (factory && !url.searchParams.has('factory')) url.searchParams.set('factory', factory);
  return <Link to={url.pathname + url.search} className={className || 'sp-link'} {...props} />;
}
export function DataTable({ headers, children, caption }: { headers: string[]; children: ReactNode; caption: string }) {
  return <div className="sp-table-scroll" tabIndex={0} aria-label={caption}><table className="sp-table"><caption className="sr-only">{caption}</caption><thead><tr>{headers.map(h => <th key={h} scope="col">{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}
export function PageHeading({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  const context = useOutletContext<SupplyContext | null>();
  if (context?.headingTarget) return <>{createPortal(<><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</>, context.headingTarget)}{action && <div className="sp-heading sp-heading-actions">{action}</div>}</>;
  return <div className="sp-heading"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{action}</div>;
}
export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="sp-empty"><CircleAlert size={28} /><h2>{title}</h2>{children}</div>;
}
