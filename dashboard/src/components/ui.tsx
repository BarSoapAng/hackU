import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { AlertCircle, ArrowLeft, ArrowRight, LoaderCircle, Search } from 'lucide-react'

export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)) }

const button = cva('inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4', {
  variants: { variant: {
    default: 'bg-primary text-primary-foreground hover:bg-primary/90',
    outline: 'border bg-card text-foreground hover:bg-muted',
    ghost: 'text-muted-foreground hover:bg-muted hover:text-foreground',
  } }, defaultVariants: { variant: 'default' },
})

export function Button({ className, variant, type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof button>) {
  return <button type={type} className={cn(button({ variant }), className)} {...props} />
}
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('rounded-xl border bg-card text-card-foreground shadow-sm', className)} {...props} />
}
export function Badge({ className, ...props }: HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn('inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1 text-sm font-medium text-muted-foreground', className)} {...props} />
}
export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn('h-11 w-full rounded-lg border border-input bg-card px-3 text-base placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring', className)} {...props} />
}
export function SearchInput({ label, ...props }: InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  return <label className="relative block w-full sm:max-w-sm"><span className="sr-only">{label}</span><Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3.5 size-4 text-muted-foreground" /><Input type="search" className="pl-10" placeholder={label} {...props} /></label>
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="px-6 py-12 text-center"><p className="font-medium">{title}</p>{children && <div className="mx-auto mt-2 max-w-lg text-sm leading-relaxed text-muted-foreground">{children}</div>}</div>
}
export function RequestState({ loading, error, retry }: { loading?: boolean; error?: string; retry: () => void }) {
  if (error) return <div role="alert" className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-destructive/30 bg-card p-5"><p className="flex items-center gap-3 text-sm"><AlertCircle className="size-5 shrink-0 text-destructive" aria-hidden="true" />{error}</p><Button variant="outline" onClick={retry}>Try again</Button></div>
  if (loading) return <div role="status" className="flex items-center justify-center gap-3 p-12 text-sm text-muted-foreground"><LoaderCircle aria-hidden="true" className="size-5 motion-safe:animate-spin" />Loading…</div>
  return null
}
export function Pagination({ offset, total, count, onChange }: { offset: number; total: number; count: number; onChange: (offset: number) => void }) {
  if (total <= 50) return null
  return <nav aria-label="User pages" className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-4"><p className="text-sm text-muted-foreground">{offset + 1}–{offset + count} of {total.toLocaleString()}</p><div className="flex gap-2"><Button variant="outline" disabled={!offset} onClick={() => onChange(Math.max(0, offset - 50))}><ArrowLeft />Previous</Button><Button variant="outline" disabled={offset + count >= total} onClick={() => onChange(offset + 50)}>Next<ArrowRight /></Button></div></nav>
}
