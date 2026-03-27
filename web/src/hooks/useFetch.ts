import { useState, useEffect, useCallback, useRef } from 'react';

interface UseFetchOptions<T> {
  immediate?: boolean;
  onSuccess?: (data: T) => void;
  onError?: (error: Error) => void;
}

export function useFetch<T>(
  asyncFn: () => Promise<T>,
  options: UseFetchOptions<T> = {}
) {
  const { immediate = true, onSuccess, onError } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(immediate);
  const [error, setError] = useState<Error | null>(null);

  // Use ref to always call the latest asyncFn without triggering effect re-runs
  const asyncFnRef = useRef(asyncFn);
  asyncFnRef.current = asyncFn;

  const onSuccessRef = useRef(onSuccess);
  onSuccessRef.current = onSuccess;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const execute = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await asyncFnRef.current();
      setData(result);
      onSuccessRef.current?.(result);
      return result;
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e));
      setError(err);
      onErrorRef.current?.(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (immediate) {
      execute();
    }
  }, [immediate, execute]);

  return { data, loading, error, refetch: execute };
}
