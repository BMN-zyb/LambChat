import { useState, useCallback } from "react";
import { getFullUrl } from "../../../services/api/config";

interface ImageWithSkeletonProps {
  /** Image source URL (will be resolved via getFullUrl if relative) */
  src?: string;
  alt?: string;
  className?: string;
  loading?: "lazy" | "eager";
  onClick?: () => void;
  /** Skip getFullUrl resolution (src is already absolute or data: URI) */
  skipUrlResolve?: boolean;
  /** Render inline without wrapper div (for thumbnails, avatars, etc.) */
  inline?: boolean;
  /** Aspect ratio for skeleton placeholder, e.g. "16/10", "1/1", "4/3" */
  aspectRatio?: string;
  /** Wrapper className for the outer container */
  wrapperClassName?: string;
  /** img element style overrides */
  style?: React.CSSProperties;
}

/**
 * Renders an <img> with a shimmer skeleton placeholder while loading.
 * Uses the shared `.skeleton-line` CSS class for consistent skeleton styling.
 *
 * Modes:
 * - Default (block): wraps in a relative container with rounded-lg shadow
 * - Inline: no wrapper, just the img + hidden skeleton — fits inside existing containers
 */
export function ImageWithSkeleton({
  src,
  alt,
  className,
  loading = "lazy",
  onClick,
  skipUrlResolve = false,
  inline = false,
  aspectRatio = "16 / 10",
  wrapperClassName,
  style,
}: ImageWithSkeletonProps) {
  const [isLoaded, setIsLoaded] = useState(false);
  const [hasError, setHasError] = useState(false);

  const resolvedSrc = skipUrlResolve ? src : getFullUrl(src);

  const handleLoad = useCallback(() => setIsLoaded(true), []);
  const handleError = useCallback(() => {
    setIsLoaded(true);
    setHasError(true);
  }, []);

  if (!resolvedSrc) return null;

  // Inline mode: skeleton sits behind the img in the same space, no extra wrapper
  if (inline) {
    return (
      <div className="relative w-full h-full">
        {!isLoaded && !hasError && (
          <div className="absolute inset-0 skeleton-line" />
        )}
        {hasError ? (
          <div className="absolute inset-0 flex items-center justify-center bg-stone-100 dark:bg-stone-800 rounded">
            <span className="text-xs text-stone-400 truncate px-1">
              {alt || "…"}
            </span>
          </div>
        ) : (
          <img
            src={resolvedSrc}
            alt={alt}
            loading={loading}
            onLoad={handleLoad}
            onError={handleError}
            onClick={onClick}
            className={className}
            referrerPolicy="no-referrer"
            style={{
              opacity: isLoaded ? 1 : 0,
              transition: isLoaded ? "opacity 0.3s ease" : "none",
              width: "100%",
              height: "100%",
              objectFit: "cover",
              ...style,
            }}
          />
        )}
      </div>
    );
  }

  // Block mode: full wrapper with skeleton, error state
  return (
    <div
      className={`relative my-2 overflow-hidden rounded-lg shadow ${
        wrapperClassName ?? ""
      }`}
    >
      {/* Skeleton placeholder */}
      {!isLoaded && !hasError && (
        <div
          className="skeleton-line w-full rounded-lg"
          style={{ aspectRatio }}
        />
      )}

      {/* Actual image */}
      {!hasError && (
        <img
          src={resolvedSrc}
          alt={alt}
          loading={loading}
          onLoad={handleLoad}
          onError={handleError}
          onClick={onClick}
          className={className}
          style={{
            opacity: isLoaded ? 1 : 0,
            transition: isLoaded ? "opacity 0.3s ease" : "none",
            maxWidth: "100%",
            height: "auto",
            width: "100%",
            cursor: onClick ? "zoom-in" : undefined,
            ...style,
          }}
        />
      )}

      {/* Error state */}
      {hasError && (
        <div
          className="flex items-center justify-center rounded-lg text-xs text-stone-400"
          style={{
            aspectRatio,
            backgroundColor: "var(--theme-bg-card, #f5f5f4)",
            border: "1px solid var(--theme-border, #e7e5e4)",
          }}
        >
          <span>{alt || "Image failed to load"}</span>
        </div>
      )}
    </div>
  );
}
