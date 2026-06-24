import React, { useRef, useState } from "react";

/**
 * Minimal fixed-row-height virtual list (no external dependency).
 * Renders only the rows visible in the viewport (+ overscan), so a 1000+ row
 * resolved-targets table never mounts all rows. Feature 3 / Feature 8.
 *
 *   <VirtualList items={rows} rowHeight={36} height={320} overscan={8}
 *                renderRow={(item, index) => <Row .../>} />
 */
export default function VirtualList({ items, rowHeight = 36, height = 320, overscan = 8, renderRow }) {
  const [scrollTop, setScrollTop] = useState(0);
  const ref = useRef(null);

  const total = items.length;
  const totalHeight = total * rowHeight;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visibleCount = Math.ceil(height / rowHeight) + overscan * 2;
  const end = Math.min(total, start + visibleCount);
  const slice = items.slice(start, end);

  return (
    <div
      ref={ref}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      style={{ height, overflowY: "auto", position: "relative" }}
      data-testid="virtual-list"
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div style={{ position: "absolute", top: start * rowHeight, left: 0, right: 0 }}>
          {slice.map((item, i) => (
            <div key={item.recipient_id || item.id || (start + i)} style={{ height: rowHeight }}>
              {renderRow(item, start + i)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
