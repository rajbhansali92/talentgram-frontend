/**
 * Centralized WhatsApp sharing message generator for Talentgram.
 */

export function generateSubmissionMessage(projectTitle, link) {
    const text = `Hello from Talentgram Agency,

*Project:* ${projectTitle}

*View Project Details & Apply:*
${link}

Kindly review the requirement and let us know if you are interested.

Thank you,

Talentgram Agency
https://www.instagram.com/talentgram.agency/`;
    return `https://wa.me/?text=${encodeURIComponent(text)}`;
}

export function generateApplicationMessage(link) {
    const text = `Hello from Talentgram Agency,

Please complete your Talentgram profile to access current and future casting opportunities.

*Complete Your Profile:*
${link}

Your profile only needs to be uploaded once and can be used for all future project applications through the same email account.

Thank you,

Talentgram Agency
https://www.instagram.com/talentgram.agency/`;
    return `https://wa.me/?text=${encodeURIComponent(text)}`;
}

export function generateClientViewMessage(projectTitle, link) {
    const text = `Hello from Talentgram Agency,

Please find below the talent presentation link for your review.

*Project:* ${projectTitle}

*View Talents:*
${link}

*Please Note:*
• Review all submitted talent profiles in one place.
• Shortlist, reject or mark preferred options as required.
• New submissions will automatically appear in the same link.
• This link will remain updated throughout the casting process.

Thank you,

Talentgram Agency
https://www.instagram.com/talentgram.agency/`;
    return `https://wa.me/?text=${encodeURIComponent(text)}`;
}
