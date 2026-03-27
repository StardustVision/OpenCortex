import React from 'react';

export const EmptyState: React.FC<{ icon: React.ReactNode; title: string; message: string; action?: React.ReactNode }> = ({ icon, title, message, action }) => (
  <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
    <div className="mb-4 text-gray-400">{icon}</div>
    <h3 className="text-lg font-medium text-gray-900">{title}</h3>
    <p className="mt-1 text-sm text-gray-500 max-w-xs">{message}</p>
    {action && <div className="mt-6">{action}</div>}
  </div>
);
