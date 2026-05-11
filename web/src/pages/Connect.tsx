import React, { useState } from 'react';
import { normalizeToken, useApi } from '../api/Context';
import { Button } from '../components/common/Button';
import { Card } from '../components/common/Card';
import { APIRequestError } from '../api/client';
import { Brain } from 'lucide-react';

export const Connect: React.FC = () => {
  const [token, setTokenInput] = useState('');
  const [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const { connect } = useApi();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const normalized = normalizeToken(token);
    if (!normalized) {
      return;
    }
    setConnecting(true);
    setError('');
    try {
      await connect(normalized);
    } catch (err) {
      if (err instanceof APIRequestError && err.status === 401) {
        setError('Token is not valid for this OpenCortex server.');
      } else {
        setError('Could not verify token. Check that the API server is running.');
      }
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <div className="flex flex-col items-center text-center mb-8">
          <div className="w-16 h-16 bg-indigo-50 rounded-2xl flex items-center justify-center mb-4">
            <Brain className="text-indigo-600" size={32} />
          </div>
          <h1 className="text-2xl font-bold text-gray-900">Connect to OpenCortex</h1>
          <p className="text-gray-500 mt-2">Enter your access token to manage your memory system.</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="token" className="block text-sm font-medium text-gray-700 mb-1">
              Access Token
            </label>
            <input
              id="token"
              type="password"
              className="w-full px-4 py-2 border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all"
              placeholder="Paste your JWT here..."
              value={token}
              onChange={(e) => setTokenInput(e.target.value)}
              required
            />
            {token && (
              <p className="mt-1 text-xs text-gray-400">
                {normalizeToken(token).length} characters after whitespace cleanup
              </p>
            )}
          </div>
          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}
          <Button type="submit" className="w-full" size="lg" loading={connecting}>
            Connect
          </Button>
        </form>
      </Card>
    </div>
  );
};
