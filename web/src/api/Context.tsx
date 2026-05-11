import React, { createContext, useContext, useEffect, useState } from 'react';
import { APIRequestError, OpenCortexClient } from './client';

type AuthStatus = 'checking' | 'authenticated' | 'anonymous';

interface ApiContextType {
  client: OpenCortexClient | null;
  token: string | null;
  role: string;
  authStatus: AuthStatus;
  connect: (token: string) => Promise<void>;
  logout: () => void;
}

const ApiContext = createContext<ApiContextType | undefined>(undefined);

const tokenStorageKey = 'opencortex_token';

export function normalizeToken(value: string): string {
  return value.replace(/\s+/g, '');
}

function tokenFromLocation(): string {
  const urlParams = new URLSearchParams(window.location.search);
  const urlToken = normalizeToken(urlParams.get('token') || '');
  if (urlToken) {
    const newUrl = window.location.pathname;
    window.history.replaceState({}, '', newUrl);
  }
  return urlToken;
}

export const ApiProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const initialToken = tokenFromLocation()
    || normalizeToken(localStorage.getItem(tokenStorageKey) || '');
  const [token, setTokenState] = useState<string | null>(initialToken || null);
  const [role, setRole] = useState('user');
  const [client, setClient] = useState<OpenCortexClient | null>(
    initialToken ? new OpenCortexClient('', initialToken) : null,
  );
  const [authStatus, setAuthStatus] = useState<AuthStatus>(
    initialToken ? 'checking' : 'anonymous',
  );

  const clearToken = () => {
    localStorage.removeItem(tokenStorageKey);
    setTokenState(null);
    setRole('user');
    setClient(null);
    setAuthStatus('anonymous');
  };

  const connect = async (nextToken: string) => {
    const normalized = normalizeToken(nextToken);
    if (!normalized) {
      clearToken();
      return;
    }
    const nextClient = new OpenCortexClient('', normalized);
    const me = await nextClient.getMe();
    localStorage.setItem(tokenStorageKey, normalized);
    setTokenState(normalized);
    setRole(me.role || 'user');
    setClient(nextClient);
    setAuthStatus('authenticated');
  };

  useEffect(() => {
    if (!initialToken) return;
    let cancelled = false;
    const bootstrap = async () => {
      try {
        const nextClient = new OpenCortexClient('', initialToken);
        const me = await nextClient.getMe();
        if (cancelled) return;
        localStorage.setItem(tokenStorageKey, initialToken);
        setRole(me.role || 'user');
        setClient(nextClient);
        setAuthStatus('authenticated');
      } catch (error) {
        if (cancelled) return;
        if (!(error instanceof APIRequestError) || error.status === 401) {
          clearToken();
          return;
        }
        clearToken();
      }
    };
    bootstrap();
    return () => {
      cancelled = true;
    };
  }, [initialToken]);

  return (
    <ApiContext.Provider value={{
      client,
      token,
      role,
      authStatus,
      connect,
      logout: clearToken,
    }}>
      {children}
    </ApiContext.Provider>
  );
};

export const useApi = () => {
  const context = useContext(ApiContext);
  if (context === undefined) {
    throw new Error('useApi must be used within an ApiProvider');
  }
  return context;
};
