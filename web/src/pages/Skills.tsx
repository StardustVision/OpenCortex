import React from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Sparkles } from 'lucide-react';

export const Skills: React.FC = () => {
  return (
    <PageLayout title="Skills">
      <div className="flex flex-col items-center justify-center py-32 text-center">
        <div className="relative mb-8">
          <div className="absolute inset-0 bg-indigo-400 blur-2xl opacity-20 animate-pulse rounded-full" />
          <div className="relative p-6 bg-white rounded-2xl border border-indigo-100 shadow-sm">
            <Sparkles size={48} className="text-indigo-500" />
          </div>
        </div>
        <h2 className="text-2xl font-bold text-gray-900 mb-2">Skills Management</h2>
        <p className="text-gray-500 max-w-md mx-auto">
          Automatic skill discovery, extraction, and management will be available in a future release.
        </p>
        <div className="mt-8 flex gap-2">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-2 w-2 rounded-full bg-indigo-100" />
          ))}
        </div>
      </div>
    </PageLayout>
  );
};
