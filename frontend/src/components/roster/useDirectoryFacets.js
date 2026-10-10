import { useEffect, useState } from "react";
import { adminApi } from "@/lib/api";

/** Tag + location options for the shared talent filters, fetched once when first needed. */
export function useDirectoryFacets(enabled) {
    const [tags, setTags] = useState([]);
    const [locations, setLocations] = useState([]);
    const [asked, setAsked] = useState(false);

    useEffect(() => { if (enabled) setAsked(true); }, [enabled]);
    useEffect(() => {
        if (!asked) return;
        adminApi.get("/tags").then(({ data }) => setTags(data?.tags || [])).catch(() => {});
        adminApi.get("/talents/facets").then(({ data }) => setLocations(data?.locations || [])).catch(() => {});
    }, [asked]);

    return { tags, locations };
}
