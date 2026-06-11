import React, { useState, useEffect, useRef } from "react";
import { X, Search, Loader2 } from "lucide-react";

export default function LocationSelector({ value = [], onChange, placeholder = "Search for a city...", testid = "location-selector", error }) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(false);
  const dropdownRef = useRef(null);

  // Debounced search logic
  useEffect(() => {
    if (!query.trim() || query.length < 2) {
      setSuggestions([]);
      return;
    }

    const delayDebounceFn = setTimeout(async () => {
      setLoading(true);
      try {
        const response = await fetch(
          `https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(
            query
          )}&addressdetails=1&limit=8`
        );
        const data = await response.json();

        // Process search results
        const items = data
          .map((item) => {
            const addr = item.address || {};
            const city =
              addr.city ||
              addr.town ||
              addr.village ||
              addr.municipality ||
              addr.county ||
              addr.state ||
              "";
            const country = addr.country || "";
            return {
              city: city.trim(),
              country: country.trim(),
            };
          })
          .filter((item) => item.city && item.country);

        // Deduplicate suggestions
        const uniqueItems = [];
        const seen = new Set();
        for (const item of items) {
          const key = `${item.city.toLowerCase()}_${item.country.toLowerCase()}`;
          if (!seen.has(key)) {
            seen.add(key);
            uniqueItems.push(item);
          }
        }

        setSuggestions(uniqueItems);
      } catch (err) {
        console.error("Error fetching locations from Nominatim:", err);
      } finally {
        setLoading(false);
      }
    }, 400); // 400ms debounce to comply with OSM usage guidelines

    return () => clearTimeout(delayDebounceFn);
  }, [query]);

  // Click outside listener to close dropdown
  useEffect(() => {
    function handleClickOutside(event) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setFocused(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleSelect = (item) => {
    const exists = value.some(
      (v) =>
        v.city.toLowerCase() === item.city.toLowerCase() &&
        v.country.toLowerCase() === item.country.toLowerCase()
    );

    if (!exists) {
      const updated = [...value, item];
      onChange(updated);
    }
    setQuery("");
    setSuggestions([]);
    setFocused(false);
  };

  const handleRemove = (indexToRemove) => {
    const updated = value.filter((_, idx) => idx !== indexToRemove);
    onChange(updated);
  };

  return (
    <div className="relative w-full" data-testid={testid} ref={dropdownRef}>
      {/* Selected Tags */}
      <div className="flex flex-wrap gap-2 mb-2">
        {value.map((loc, idx) => (
          <div
            key={idx}
            data-testid={`location-chip-${idx}`}
            className="inline-flex items-center gap-1.5 bg-[#0c2340]/5 border border-[#0c2340]/10 text-[#0c2340] rounded-full py-1 px-3 text-[13px] font-medium transition-all duration-150 hover:bg-[#0c2340]/10"
          >
            <span>
              {loc.city}, {loc.country}
            </span>
            <button
              type="button"
              onClick={() => handleRemove(idx)}
              data-testid={`remove-location-btn-${idx}`}
              className="hover:bg-black/10 rounded-full p-0.5 text-[#0c2340]/70 hover:text-black transition-colors"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ))}
      </div>

      {/* Input container */}
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setFocused(true)}
          placeholder={placeholder}
          data-testid="location-search-input"
          className={`w-full bg-white/60 rounded-2xl border ${
            error ? "border-red-500" : "border-[#eaeaea]"
          } focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3.5 pl-11 pr-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]`}
        />
        <div className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400">
          {loading ? (
            <Loader2 className="w-4 h-4 animate-spin text-[#0c2340]" />
          ) : (
            <Search className="w-4 h-4 text-[#0c2340]" />
          )}
        </div>
      </div>

      {/* Error message */}
      {error && (
        <p className="text-red-500 text-xs mt-1.5 font-medium pl-1">{error}</p>
      )}

      {/* Autocomplete Dropdown */}
      {focused && suggestions.length > 0 && (
        <div
          data-testid="location-suggestions-dropdown"
          className="absolute z-50 w-full mt-2 bg-white border border-[#eaeaea] rounded-2xl shadow-xl max-h-60 overflow-y-auto animate-fadeIn overflow-hidden"
        >
          {suggestions.map((item, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => handleSelect(item)}
              data-testid={`suggestion-item-${idx}`}
              className="w-full text-left px-4 py-3 text-[14px] text-slate-800 hover:bg-[#0c2340]/5 transition-colors border-b border-slate-50 last:border-0 font-medium"
            >
              {item.city}, {item.country}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
