import { REQUIREMENT_TIERS } from "@/lib/readinessStatus";

// THE REQUIREMENT ENGINE — single source of truth for "what is required,
// optional, or hidden, and is it satisfied yet." Pure function of
// `project.submission_requirements` + current `form`/`submission` state.
// It ONLY decides two things per item, always using the REQUIREMENT_TIERS
// enum (never a raw string):
//   - `requirement`: REQUIREMENT_TIERS.REQUIRED | .OPTIONAL | .HIDDEN
//   - `satisfied`: does current form/media data meet the configured rule?
//
// It never looks at the Upload Manager — live upload state
// (OPERATIONAL_STATES.UPLOADING/PROCESSING/FAILED/…) is a completely
// separate concern, combined in later by the Operational Engine's
// `deriveOperationalStatus` (see lib/readinessStatus.js). Keeping the two
// axes independent is what lets the readiness panel show "Required +
// Uploading" instead of a premature "Missing" while a file is in flight.
//
// Nothing here is hardcoded business logic: every item only exists because
// the admin's config says so. This module has no React dependency — it's a
// plain function of its three inputs, callable from a hook (see
// hooks/useSubmissionExperienceModel.js), a test, or anywhere else that has
// `project`/`form`/`submission` in hand.
const SKILLS_CATEGORIES = {
    "language": ["English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Urdu", "Other"],
    "languages": ["English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Urdu", "Other"],
    "performance": ["Actor", "Voice Artist", "Dancer", "Singer", "Host", "Anchor", "Model", "Theatre Artist", "Improvisation", "Stand-up Comedy"],
    "sports": ["Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket", "Football", "Basketball", "Tennis", "Badminton"],
    "sports & fitness": ["Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket", "Football", "Basketball", "Tennis", "Badminton"],
    "action": ["Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu", "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour", "Sword Fighting"],
    "action & stunts": ["Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu", "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour", "Sword Fighting"],
    "vehicle": ["Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle", "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat", "Ride Jet Ski"],
    "vehicle skills": ["Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle", "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat", "Ride Jet Ski"],
    "special": ["Skateboarding", "Roller Skating", "Ice Skating", "Surfing", "Scuba Diving", "Fire Performance", "Juggling"],
    "special skills": ["Skateboarding", "Roller Skating", "Ice Skating", "Surfing", "Scuba Diving", "Fire Performance", "Juggling"],
    "dance": ["Hip Hop", "Contemporary", "Bollywood", "Bharatanatyam", "Kathak", "Salsa", "Ballet"],
    "music": ["Singer", "Piano", "Keyboard", "Guitar", "Violin", "Drums", "Flute", "Ukulele", "DJ", "Beatboxing", "Rapper", "Composer", "Music Producer"],
};

export function computeRequirementItems({ project, form, submission }) {
    const items = [];
    const requirements = project?.submission_requirements;
    if (!requirements) {
        // Fallback legacy validation rules — everything here is required.
        items.push({ id: "first_name", label: "First name", section: "profile", selector: '[data-testid="form-first-name"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!form.first_name?.trim() });
        items.push({ id: "last_name", label: "Last name", section: "profile", selector: '[data-testid="form-last-name"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!form.last_name?.trim() });
        items.push({ id: "height", label: "Height", section: "profile", selector: '[data-testid="form-height-field"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!form.height?.trim() });
        items.push({ id: "location", label: "Current location", section: "profile", selector: '[data-testid="form-location"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!(form.location && form.location.length > 0) });

        const avail = form.availability || {};
        const status = (avail.status || "").trim();
        if (status !== "yes" && status !== "no") {
            items.push({ id: "availability", label: "Availability (Yes / No)", section: "profile", selector: '[data-testid="availability-block"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: false });
        } else {
            items.push({ id: "availability", label: "Availability (Yes / No)", section: "profile", selector: '[data-testid="availability-block"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: true });
            if (status === "no") {
                items.push({ id: "availability_note", label: "Availability note", section: "profile", selector: '[data-testid="availability-note-input"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!(avail.note || "").trim() });
            }
        }

        const budget = form.budget || {};
        const bstatus = (budget.status || "").trim();
        if (bstatus !== "accept" && bstatus !== "custom") {
            items.push({ id: "budget", label: "Budget (Accept / Custom)", section: "profile", selector: '[data-testid="budget-block"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: false });
        } else {
            items.push({ id: "budget", label: "Budget (Accept / Custom)", section: "profile", selector: '[data-testid="budget-block"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: true });
            if (bstatus === "custom") {
                items.push({ id: "budget_value", label: "Expected budget details", section: "profile", selector: '[data-testid="budget-value-input"]', requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: !!(budget.value || "").trim() });
            }
        }
        return items;
    }

    if (requirements.strictness !== "strict") {
        return [];
    }

    const fieldsConfig = requirements.fields || {};
    // Profile fields in this engine are two-tier only (no per-field
    // "hidden" — see 04_MEDIA_RULES/audit); anything not explicitly
    // "required" is optional but still rendered.
    const fieldTier = (key) => (fieldsConfig[key] === REQUIREMENT_TIERS.REQUIRED ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL);

    // 1. Standard Profile Fields
    items.push({ id: "first_name", label: "First name", section: "profile", selector: '[data-testid="form-first-name"]', requirement: fieldTier("name"), satisfied: !!form.first_name?.trim() });
    items.push({ id: "last_name", label: "Last name", section: "profile", selector: '[data-testid="form-last-name"]', requirement: fieldTier("name"), satisfied: !!form.last_name?.trim() });
    items.push({ id: "email", label: "Email", section: "profile", selector: '[data-testid="form-email"]', requirement: fieldTier("email"), satisfied: !!(submission?.talent_email || form.email)?.trim() });
    items.push({ id: "phone", label: "Phone", section: "profile", selector: '[data-testid="form-phone"]', requirement: fieldTier("phone"), satisfied: !!form.phone?.trim() });
    items.push({ id: "dob", label: "Date of Birth", section: "profile", selector: '[data-testid="form-dob"]', requirement: fieldTier("dob"), satisfied: !!form.dob?.trim() });
    items.push({ id: "age", label: "Age", section: "profile", selector: '[data-testid="form-age-field"]', requirement: fieldTier("age"), satisfied: !(form.age === undefined || form.age === null || String(form.age).trim() === "") });
    items.push({ id: "height", label: "Height", section: "profile", selector: '[data-testid="form-height-field"]', requirement: fieldTier("height"), satisfied: !!form.height?.trim() });
    items.push({ id: "location", label: "Current location", section: "profile", selector: '[data-testid="form-location"]', requirement: fieldTier("location"), satisfied: !!(form.location && form.location.length > 0) });
    items.push({ id: "gender", label: "Gender", section: "profile", selector: '[data-testid="form-gender-field"]', requirement: fieldTier("gender"), satisfied: !!form.gender?.trim() });
    items.push({ id: "ethnicity", label: "Ethnicity", section: "profile", selector: '[data-testid="form-ethnicity-field"]', requirement: fieldTier("ethnicity"), satisfied: !!form.ethnicity?.trim() });
    items.push({ id: "instagram_handle", label: "Instagram Handle", section: "profile", selector: '[data-testid="form-instagram-handle"]', requirement: fieldTier("instagram_handle"), satisfied: !!form.instagram_handle?.trim() });
    items.push({ id: "instagram_followers", label: "Instagram Followers", section: "profile", selector: '[data-testid="form-instagram-followers-field"]', requirement: fieldTier("instagram_followers"), satisfied: !!form.instagram_followers?.trim() });
    items.push({ id: "bio", label: "Bio", section: "profile", selector: '[data-testid="form-bio-field"]', requirement: fieldTier("bio"), satisfied: !!form.bio?.trim() });
    items.push({ id: "competitive_brand", label: "Competitive Brand details", section: "projectQuestions", selector: '[data-testid="form-competitive-brand"]', requirement: fieldTier("competitive_brand"), satisfied: !!form.competitive_brand?.trim() });

    {
        const avail = form.availability || {};
        const status = (avail.status || "").trim();
        const tier = fieldTier("availability");
        if (status !== "yes" && status !== "no") {
            items.push({ id: "availability", label: "Availability (Yes / No)", section: "projectQuestions", selector: '[data-testid="availability-block"]', requirement: tier, satisfied: false });
        } else {
            items.push({ id: "availability", label: "Availability (Yes / No)", section: "projectQuestions", selector: '[data-testid="availability-block"]', requirement: tier, satisfied: true });
            if (status === "no") {
                items.push({ id: "availability_note", label: "Availability note", section: "projectQuestions", selector: '[data-testid="availability-note-input"]', requirement: tier, satisfied: !!(avail.note || "").trim() });
            }
        }
    }

    {
        const budget = form.budget || {};
        const bstatus = (budget.status || "").trim();
        const tier = fieldTier("budget_expectation");
        if (bstatus !== "accept" && bstatus !== "custom") {
            items.push({ id: "budget", label: "Budget (Accept / Custom)", section: "projectQuestions", selector: '[data-testid="budget-block"]', requirement: tier, satisfied: false });
        } else {
            items.push({ id: "budget", label: "Budget (Accept / Custom)", section: "projectQuestions", selector: '[data-testid="budget-block"]', requirement: tier, satisfied: true });
            if (bstatus === "custom") {
                items.push({ id: "budget_value", label: "Expected budget details", section: "projectQuestions", selector: '[data-testid="budget-value-input"]', requirement: tier, satisfied: !!(budget.value || "").trim() });
            }
        }
    }

    items.push({ id: "interested_in", label: "Casting Interests", section: "profile", selector: '[data-testid="interested-in-section"]', requirement: requirements.interested_in === REQUIREMENT_TIERS.REQUIRED ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL, satisfied: !!(form.interested_in && form.interested_in.length > 0) });

    // 2. Custom Questions — only questions the project actually defines are
    // enumerable, so (unlike the fixed profile fields above) this still
    // iterates rather than listing every possible id.
    const customReqs = requirements.custom_questions || {};
    const customAnswers = form.custom_answers || {};
    (project?.custom_questions || []).forEach(cq => {
        if (!cq.id) return;
        items.push({
            id: `cq_${cq.id}`,
            label: `"${cq.question}" answers`,
            section: "projectQuestions",
            selector: `[data-testid="form-cq-${cq.id}"]`,
            requirement: customReqs[cq.id] === REQUIREMENT_TIERS.REQUIRED ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL,
            satisfied: !!String(customAnswers[cq.id] || "").trim(),
        });
    });

    // 3. Media Uploads — each of these already carries a real 3-state
    // visibility (required/optional/hidden) in project config, so the tier
    // is read straight from it rather than collapsed to a boolean.
    const mediaList = submission?.media || [];
    {
        const introVis = requirements.intro_video === REQUIREMENT_TIERS.HIDDEN ? REQUIREMENT_TIERS.HIDDEN : requirements.intro_video === REQUIREMENT_TIERS.REQUIRED ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL;
        const hasIntro = mediaList.some(m => m.category === "intro_video");
        items.push({ id: "intro_video", label: "Introduction Video", section: "uploads", selector: '[data-testid="uploads-section"]', requirement: introVis, satisfied: hasIntro, media: { prefix: "intro_video" } });
    }

    const minTakes = parseInt(requirements.min_audition_takes || 0, 10);
    const takesVis = requirements.audition_takes_visibility || (minTakes > 0 ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL);
    {
        const takesCount = mediaList.filter(m => ["take", "take_1", "take_2", "take_3"].includes(m.category)).length;
        items.push({ id: "takes", label: `Audition Takes (minimum ${minTakes})`, section: "uploads", selector: '[data-testid="takes-section"]', requirement: takesVis, satisfied: takesCount >= minTakes, media: { prefix: "take" } });
    }

    const portfolioReqs = requirements.portfolio || {};
    const portfolioCats = [
        { category: "image", label: "Portfolio (General)", selector: '[data-testid="portfolio-group-generic"]', visKey: "portfolio_image_visibility" },
        { category: "indian", label: "Indian Look", selector: '[data-testid="portfolio-group-indian"]', visKey: "portfolio_indian_visibility" },
        { category: "western", label: "Western Look", selector: '[data-testid="portfolio-group-western"]', visKey: "portfolio_western_visibility" },
    ];
    portfolioCats.forEach(cat => {
        const minCount = parseInt(portfolioReqs[cat.category] || 0, 10);
        const pVis = requirements[cat.visKey] || (minCount > 0 ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL);
        const count = mediaList.filter(m => m.category === cat.category).length;
        items.push({
            id: `portfolio_${cat.category}`,
            label: `${cat.label} (minimum ${minCount})`,
            section: "uploads",
            selector: cat.selector,
            requirement: pVis,
            satisfied: count >= minCount,
            media: { prefix: `${cat.category}:` },
        });
    });

    // 4. Work Links
    const minLinks = parseInt(requirements.min_work_links || 0, 10);
    const linksVis = requirements.work_links_visibility || (minLinks > 0 ? REQUIREMENT_TIERS.REQUIRED : REQUIREMENT_TIERS.OPTIONAL);
    {
        const linksCount = (form.work_links || []).length;
        items.push({ id: "work_links", label: `Work Links (minimum ${minLinks})`, section: "profile", selector: '[data-testid="form-work-links-field"]', requirement: linksVis, satisfied: linksCount >= minLinks });
    }

    // 5. Skills & Special Abilities — unlike the fields above, there is no
    // enumerable "all possible skill categories" in config to list as
    // optional; a category only exists as a requirement item at all when the
    // project has explicitly turned it on.
    const skillsReqs = requirements.skills || {};
    const userSkills = form.skills || [];
    Object.keys(skillsReqs).forEach(cat => {
        if (skillsReqs[cat]) {
            const validSkills = SKILLS_CATEGORIES[cat.toLowerCase()] || [];
            const hasSkill = userSkills.some(s => validSkills.includes(s));
            items.push({
                id: `skills_${cat}`,
                label: `At least one skill from category "${cat}"`,
                section: "profile",
                selector: '[data-testid="form-skills-field"]',
                requirement: REQUIREMENT_TIERS.REQUIRED,
                satisfied: hasSkill,
            });
        }
    });

    // 6. Conditional Rules
    const getMediaLabel = (m) => {
        if (m.label) return m.label;
        if (m.category === "intro_video") return "Introduction Video";
        if (m.category === "take_1") return "Take 1";
        if (m.category === "take_2") return "Take 2";
        if (m.category === "take_3") return "Take 3";
        return "";
    };
    const conditionalRules = requirements.conditional_rules || [];
    conditionalRules.forEach(rule => {
        const qid = rule.question_id;
        const trigger = rule.trigger_value;
        const videoLabel = rule.video_label;
        if (qid && trigger && videoLabel) {
            const ans = String(customAnswers[qid] || "").trim().toLowerCase();
            if (ans === String(trigger).trim().toLowerCase()) {
                const hasCondVideo = mediaList.some(m =>
                    ["take", "intro_video", "take_1", "take_2", "take_3"].includes(m.category) &&
                    getMediaLabel(m).trim().toLowerCase() === videoLabel.trim().toLowerCase()
                );
                const cqObj = (project?.custom_questions || []).find(q => q.id === qid);
                const questionText = cqObj ? cqObj.question : "additional question";
                items.push({
                    id: `conditional_${qid}_${videoLabel}`,
                    label: `"${videoLabel}" required (Because you answered "${trigger}" to "${questionText}")`,
                    section: "uploads",
                    selector: '[data-testid="uploads-section"]',
                    requirement: REQUIREMENT_TIERS.REQUIRED,
                    satisfied: hasCondVideo,
                    media: { prefix: "take" },
                });
            }
        }
    });

    return items;
}
