import { MutableRefObject, useEffect } from 'react';

export function useIntersectionObserver(
  ref: MutableRefObject<Element | null>,
  callback: () => void,
  options?: IntersectionObserverInit,
) {
  useEffect(() => {
    if (!ref.current) return;
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          callback();
        }
      });
    }, options);
    const element = ref.current;
    observer.observe(element);
    return () => {
      observer.unobserve(element);
      observer.disconnect();
    };
  }, [ref, callback, options]);
}
