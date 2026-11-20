import { Activity } from 'lucide-react';

export function AIHealthPlaceholderPage() {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center max-w-2xl mx-auto mt-8">
      <Activity className="mx-auto h-10 w-10 text-indigo-500" />
      <h1 className="mt-3 text-lg font-bold text-slate-900">
        AI Activity &amp; Health (Slice 5)
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        Agent trace feed, Lex Draft Trust Metrics (autonomy policy approval rates), and the 1-click YAML Eval Runner scorecard will be wired here in Slice 5.
      </p>
    </div>
  );
}
