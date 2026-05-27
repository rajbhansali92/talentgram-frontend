import React, { useEffect, useState, useRef } from "react";
import { adminApi, getAdmin } from "@/lib/api";
import {
    Plus,
    Trash2,
    Check,
    MessageSquare,
    ChevronDown,
    ChevronUp,
    Bell,
    Loader2,
    Send,
    Link2,
    Image as ImageIcon,
    AlertCircle,
    Instagram,
    Phone,
    User,
    Calendar,
    ListTodo,
    CheckSquare,
    DollarSign,
    Briefcase,
    Settings,
    X,
} from "lucide-react";
import { toast } from "sonner";

export default function WorkflowPage() {
    const admin = getAdmin();
    const isAdmin = admin?.role === "admin";
    const currentUserId = admin?.id;

    // List states
    const [tasks, setTasks] = useState([]);
    const [scouts, setScouts] = useState([]);
    const [users, setUsers] = useState([]);
    const [projects, setProjects] = useState([]);
    const [notifications, setNotifications] = useState([]);
    const [loadingTasks, setLoadingTasks] = useState(true);
    const [loadingScouts, setLoadingScouts] = useState(true);

    // Expand states for task feed
    const [expandedTaskId, setExpandedTaskId] = useState(null);

    // Filter controls for task feed
    const [categoryFilter, setCategoryFilter] = useState("all"); // all | general | project | scouting | finance
    const [statusFilter, setStatusFilter] = useState("active"); // active (pending/in_progress) | completed | archived | all

    // Task Create Form states
    const [showNewTaskForm, setShowNewTaskForm] = useState(false);
    const [newTask, setNewTask] = useState({
        title: "",
        description: "",
        category: "general",
        assignee_id: "",
        project_id: "",
    });

    // Subtask input states (mapped by taskId)
    const [subtaskInputs, setSubtaskInputs] = useState({});

    // Comment input states (mapped by taskId)
    const [commentInputs, setCommentInputs] = useState({});
    const [commentAttachmentUrl, setCommentAttachmentUrl] = useState({});
    const [commentAttachmentType, setCommentAttachmentType] = useState({}); // link | image

    // Scouting Entry Form states
    const [newScout, setNewScout] = useState({
        instagram_link: "",
        phone: "",
        name: "",
        notes: "",
        assigned_id: "",
    });

    // Fetch lists
    const fetchTasks = async () => {
        try {
            const { data } = await adminApi.get("/workflow/tasks");
            setTasks(data || []);
        } catch (e) {
            console.error("Failed to fetch tasks:", e);
        } finally {
            setLoadingTasks(false);
        }
    };

    const fetchScouts = async () => {
        try {
            const { data } = await adminApi.get("/workflow/scouting");
            setScouts(data || []);
        } catch (e) {
            console.error("Failed to fetch scouts:", e);
        } finally {
            setLoadingScouts(false);
        }
    };

    const fetchUsers = async () => {
        try {
            const { data } = await adminApi.get("/users");
            setUsers(data?.items || []);
        } catch (e) {
            console.error("Failed to fetch users:", e);
        }
    };

    const fetchProjects = async () => {
        try {
            const { data } = await adminApi.get("/projects");
            setProjects(data || []);
        } catch (e) {
            console.error("Failed to fetch projects:", e);
        }
    };

    const fetchNotifications = async () => {
        try {
            const { data } = await adminApi.get("/workflow/notifications");
            setNotifications(data || []);
        } catch (e) {
            console.error("Failed to fetch notifications:", e);
        }
    };

    // Polling setup for notifications and lightweight list refresh
    useEffect(() => {
        fetchTasks();
        fetchScouts();
        fetchUsers();
        fetchProjects();
        fetchNotifications();

        const intervalId = setInterval(() => {
            fetchNotifications();
            // Silence refreshing of lists to prevent layout jumps on active typing
        }, 20000);

        return () => clearInterval(intervalId);
    }, []);

    // --------------------------------------------------------------------------
    // Task Operations
    // --------------------------------------------------------------------------
    const handleCreateTask = async (e) => {
        e.preventDefault();
        if (!newTask.title.trim()) return;

        try {
            const payload = {
                title: newTask.title.trim(),
                description: newTask.description.trim(),
                category: newTask.category,
                assignee_id: newTask.assignee_id || null,
                project_id: newTask.project_id || null,
                subtasks: [],
                attachments: [],
            };
            const { data } = await adminApi.post("/workflow/tasks", payload);
            setTasks([data, ...tasks]);
            setNewTask({
                title: "",
                description: "",
                category: "general",
                assignee_id: "",
                project_id: "",
            });
            setShowNewTaskForm(false);
            toast.success("Task logged successfully");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create task");
        }
    };

    const handleUpdateTaskStatus = async (taskId, newStatus) => {
        try {
            const { data } = await adminApi.put(`/workflow/tasks/${taskId}`, { status: newStatus });
            setTasks(tasks.map((t) => (t.id === taskId ? data : t)));
            toast.success(`Task status updated to ${newStatus}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Status update failed");
        }
    };

    const handleUpdateTaskAssignee = async (taskId, assigneeId) => {
        try {
            const { data } = await adminApi.put(`/workflow/tasks/${taskId}`, { assignee_id: assigneeId || null });
            setTasks(tasks.map((t) => (t.id === taskId ? data : t)));
            toast.success("Assignee updated");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to update assignee");
        }
    };

    const handleDeleteTask = async (taskId) => {
        if (!window.confirm("Are you sure you want to delete this task?")) return;
        try {
            await adminApi.delete(`/workflow/tasks/${taskId}`);
            setTasks(tasks.filter((t) => t.id !== taskId));
            toast.success("Task expunged");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Deletion failed");
        }
    };

    // --------------------------------------------------------------------------
    // Subtask Checklist Operations
    // --------------------------------------------------------------------------
    const handleAddSubtask = async (taskId) => {
        const text = subtaskInputs[taskId]?.trim();
        if (!text) return;

        const task = tasks.find((t) => t.id === taskId);
        if (!task) return;

        const newSubtask = {
            id: Math.random().toString(36).substr(2, 9),
            text,
            completed: false,
            completed_at: null,
        };

        const updatedSubtasks = [...(task.subtasks || []), newSubtask];

        try {
            const { data } = await adminApi.put(`/workflow/tasks/${taskId}`, { subtasks: updatedSubtasks });
            setTasks(tasks.map((t) => (t.id === taskId ? data : t)));
            setSubtaskInputs({ ...subtaskInputs, [taskId]: "" });
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to add subtask");
        }
    };

    const handleToggleSubtask = async (taskId, subtaskId) => {
        const task = tasks.find((t) => t.id === taskId);
        if (!task) return;

        const updatedSubtasks = (task.subtasks || []).map((s) => {
            if (s.id === subtaskId) {
                const nextCompleted = !s.completed;
                return {
                    ...s,
                    completed: nextCompleted,
                    completed_at: nextCompleted ? new Date().toISOString() : null,
                };
            }
            return s;
        });

        try {
            const { data } = await adminApi.put(`/workflow/tasks/${taskId}`, { subtasks: updatedSubtasks });
            setTasks(tasks.map((t) => (t.id === taskId ? data : t)));
        } catch (e) {
            toast.error("Failed to toggle subtask");
        }
    };

    const handleRemoveSubtask = async (taskId, subtaskId) => {
        const task = tasks.find((t) => t.id === taskId);
        if (!task) return;

        const updatedSubtasks = (task.subtasks || []).filter((s) => s.id !== subtaskId);

        try {
            const { data } = await adminApi.put(`/workflow/tasks/${taskId}`, { subtasks: updatedSubtasks });
            setTasks(tasks.map((t) => (t.id === taskId ? data : t)));
        } catch (e) {
            toast.error("Failed to remove subtask");
        }
    };

    // --------------------------------------------------------------------------
    // Comment Operations
    // --------------------------------------------------------------------------
    const handleAddComment = async (taskId) => {
        const text = commentInputs[taskId]?.trim();
        const attachUrl = commentAttachmentUrl[taskId]?.trim();
        const attachType = commentAttachmentType[taskId] || "link";

        if (!text && !attachUrl) return;

        const attachments = [];
        if (attachUrl) {
            attachments.push({
                type: attachType,
                url: attachUrl,
                name: attachType === "link" ? "Link attachment" : "Image attachment",
            });
        }

        try {
            const { data } = await adminApi.post(`/workflow/tasks/${taskId}/comments`, {
                text: text || "Attachment shared",
                attachments,
            });

            // Update task in local state with the returned comment
            setTasks(tasks.map((t) => {
                if (t.id === taskId) {
                    return {
                        ...t,
                        comments: [...(t.comments || []), data],
                        updated_at: new Date().toISOString(),
                    };
                }
                return t;
            }));

            // Clear inputs
            setCommentInputs({ ...commentInputs, [taskId]: "" });
            setCommentAttachmentUrl({ ...commentAttachmentUrl, [taskId]: "" });
        } catch (e) {
            toast.error("Failed to send comment");
        }
    };

    // --------------------------------------------------------------------------
    // Scouting Queue Operations
    // --------------------------------------------------------------------------
    const handleCreateScout = async (e) => {
        e.preventDefault();
        if (!newScout.instagram_link.trim() || !newScout.phone.trim()) {
            toast.error("Instagram profile link & Phone number required");
            return;
        }

        try {
            const { data } = await adminApi.post("/workflow/scouting", {
                instagram_link: newScout.instagram_link.trim(),
                phone: newScout.phone.trim(),
                name: newScout.name.trim() || null,
                notes: newScout.notes.trim() || null,
                assigned_id: newScout.assigned_id || null,
                status: "not_contacted",
            });

            setScouts([data, ...scouts]);
            setNewScout({
                instagram_link: "",
                phone: "",
                name: "",
                notes: "",
                assigned_id: "",
            });
            toast.success("Scouting log added");
        } catch (e) {
            toast.error("Scout entry creation failed");
        }
    };

    const handleUpdateScoutStatus = async (scoutId, nextStatus) => {
        try {
            const { data } = await adminApi.put(`/workflow/scouting/${scoutId}`, { status: nextStatus });
            setScouts(scouts.map((s) => (s.id === scoutId ? data : s)));
            toast.success("Scouting status updated");
        } catch (e) {
            toast.error("Scout update failed");
        }
    };

    const handleUpdateScoutNotes = async (scoutId, nextNotes) => {
        try {
            const { data } = await adminApi.put(`/workflow/scouting/${scoutId}`, { notes: nextNotes });
            setScouts(scouts.map((s) => (s.id === scoutId ? data : s)));
            toast.success("Notes saved");
        } catch (e) {
            toast.error("Failed to update notes");
        }
    };

    const handleDeleteScout = async (scoutId) => {
        if (!window.confirm("Delete this scout entry?")) return;
        try {
            await adminApi.delete(`/workflow/scouting/${scoutId}`);
            setScouts(scouts.filter((s) => s.id !== scoutId));
            toast.success("Scouting entry deleted");
        } catch (e) {
            toast.error("Delete failed");
        }
    };

    // --------------------------------------------------------------------------
    // Workflow Notifications Operations
    // --------------------------------------------------------------------------
    const handleClearNotifications = async () => {
        try {
            await adminApi.post("/workflow/notifications/read-all");
            setNotifications([]);
            toast.success("All alerts cleared");
        } catch (e) {
            toast.error("Failed to clear alerts");
        }
    };

    // Helpers
    const getAssigneeName = (uid) => {
        if (!uid) return "Unassigned";
        if (uid === currentUserId) return "You";
        const found = users.find((u) => u.id === uid);
        return found ? found.name : "Team Member";
    };

    const getAssigneeInitials = (uid) => {
        if (!uid) return "?";
        const found = users.find((u) => u.id === uid);
        if (!found) return "T";
        return found.name.split(" ").map((n) => n[0]).join("").toUpperCase().substring(0, 2);
    };

    const getProjectName = (pid) => {
        if (!pid) return "";
        const found = projects.find((p) => p.id === pid);
        return found ? found.title : "Ref Project";
    };

    const filteredTasks = tasks.filter((t) => {
        if (categoryFilter !== "all" && t.category !== categoryFilter) return false;
        if (statusFilter === "active" && (t.status === "completed" || t.status === "archived")) return false;
        if (statusFilter === "completed" && t.status !== "completed") return false;
        if (statusFilter === "archived" && t.status !== "archived") return false;
        return true;
    });

    const getCategoryIcon = (category) => {
        switch (category) {
            case "finance":
                return <DollarSign className="w-3.5 h-3.5" />;
            case "scouting":
                return <Instagram className="w-3.5 h-3.5" />;
            case "project":
                return <Briefcase className="w-3.5 h-3.5" />;
            default:
                return <ListTodo className="w-3.5 h-3.5" />;
        }
    };

    return (
        <div className="space-y-4 max-w-6xl">
            {/* 1. Isolated Notifications Bar */}
            {notifications.length > 0 && (
                <div className="flex items-center justify-between bg-black text-white px-4 py-2.5 rounded-sm shadow-sm gap-4 text-xs font-mono tracking-tight shrink-0">
                    <div className="flex items-center gap-2 overflow-hidden">
                        <Bell className="w-3.5 h-3.5 animate-pulse text-emerald-400 shrink-0" />
                        <span className="truncate">
                            <strong className="uppercase mr-1">Activity Alert:</strong>
                            {notifications[0].title}
                        </span>
                        {notifications.length > 1 && (
                            <span className="bg-white/20 px-1 py-0.5 rounded-sm shrink-0">
                                +{notifications.length - 1} more
                            </span>
                        )}
                    </div>
                    <button
                        onClick={handleClearNotifications}
                        className="underline hover:text-white/80 shrink-0 font-bold focus:outline-none"
                    >
                        Clear
                    </button>
                </div>
            )}

            {/* Title Section */}
            <div className="flex items-center justify-between gap-4 flex-wrap border-b border-black/[0.06] pb-4">
                <div>
                    <p className="eyebrow mb-1">Operations Console</p>
                    <h1 className="font-display text-2xl md:text-3xl tracking-tight text-black/90">
                        Workflow & Scouting Queue
                    </h1>
                </div>
                <button
                    onClick={() => setShowNewTaskForm(!showNewTaskForm)}
                    className="px-3.5 py-2 bg-black text-white hover:bg-black/95 rounded-sm text-xs font-medium inline-flex items-center gap-1.5 focus:outline-none"
                >
                    <Plus className="w-3.5 h-3.5" />
                    New Task
                </button>
            </div>

            {/* New Task Inline Form */}
            {showNewTaskForm && (
                <form
                    onSubmit={handleCreateTask}
                    className="border border-black/[0.08] bg-white rounded-md p-4 space-y-3"
                >
                    <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Create New Task</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-black/55">Task Title</label>
                            <input
                                type="text"
                                value={newTask.title}
                                onChange={(e) => setNewTask({ ...newTask, title: e.target.value })}
                                placeholder="Enter operational task title..."
                                className="w-full text-xs px-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30"
                                required
                            />
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold text-black/55">Category</label>
                                <select
                                    value={newTask.category}
                                    onChange={(e) => setNewTask({ ...newTask, category: e.target.value })}
                                    className="w-full text-xs px-2 py-1.5 border border-black/[0.08] rounded-sm bg-white focus:outline-none"
                                >
                                    <option value="general">General</option>
                                    <option value="project">Project</option>
                                    <option value="scouting">Scouting</option>
                                    <option value="finance">Finance</option>
                                </select>
                            </div>
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold text-black/55">Assignee</label>
                                <select
                                    value={newTask.assignee_id}
                                    onChange={(e) => setNewTask({ ...newTask, assignee_id: e.target.value })}
                                    className="w-full text-xs px-2 py-1.5 border border-black/[0.08] rounded-sm bg-white focus:outline-none"
                                >
                                    <option value="">Select Assignee...</option>
                                    {users.map((u) => (
                                        <option key={u.id} value={u.id}>
                                            {u.name}
                                        </option>
                                    ))}
                                </select>
                            </div>
                        </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-black/55">Description</label>
                            <textarea
                                value={newTask.description}
                                onChange={(e) => setNewTask({ ...newTask, description: e.target.value })}
                                placeholder="Operational details / target updates..."
                                rows={2}
                                className="w-full text-xs px-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30"
                            />
                        </div>
                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-black/55">Project Reference</label>
                            <select
                                value={newTask.project_id}
                                onChange={(e) => setNewTask({ ...newTask, project_id: e.target.value })}
                                className="w-full text-xs px-2 py-1.5 border border-black/[0.08] rounded-sm bg-white focus:outline-none"
                            >
                                <option value="">Select Associated Project...</option>
                                {projects.map((p) => (
                                    <option key={p.id} value={p.id}>
                                        {p.title}
                                    </option>
                                ))}
                            </select>
                        </div>
                    </div>
                    <div className="flex justify-end gap-2 pt-2">
                        <button
                            type="button"
                            onClick={() => setShowNewTaskForm(false)}
                            className="px-3 py-1.5 border border-black/[0.08] rounded-sm text-xs font-medium text-black/60 hover:bg-black/[0.02]"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            className="px-3.5 py-1.5 bg-black text-white hover:bg-black/95 rounded-sm text-xs font-medium"
                        >
                            Log Task
                        </button>
                    </div>
                </form>
            )}

            {/* Split Operations Workspace Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 items-start">
                
                {/* 2. Left Column: Workflow Tasks Feed (7 Columns) */}
                <div className="lg:col-span-7 space-y-4">
                    {/* Header toolbar controls */}
                    <div className="flex items-center justify-between gap-2 border border-black/[0.06] bg-white p-3 rounded-md shadow-sm">
                        <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px] uppercase font-bold text-black/45 mr-1">Category:</span>
                            {["all", "general", "project", "scouting", "finance"].map((cat) => (
                                <button
                                    key={cat}
                                    onClick={() => setCategoryFilter(cat)}
                                    className={`px-2.5 py-1 text-[11px] font-medium rounded-sm border uppercase transition-all ${
                                        categoryFilter === cat
                                            ? "bg-black text-white border-black"
                                            : "text-black/60 hover:text-black hover:bg-black/[0.02] border-black/[0.06]"
                                    }`}
                                >
                                    {cat}
                                </button>
                            ))}
                        </div>
                        <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px] uppercase font-bold text-black/45 mr-1">Status:</span>
                            <select
                                value={statusFilter}
                                onChange={(e) => setStatusFilter(e.target.value)}
                                className="text-xs border border-black/[0.06] rounded-sm py-1 px-2 focus:outline-none"
                            >
                                <option value="active">Active Tasks</option>
                                <option value="completed">Completed Only</option>
                                <option value="archived">Archived Only</option>
                                <option value="all">All Tasks</option>
                            </select>
                        </div>
                    </div>

                    {/* Tasks Feed Content */}
                    <div className="space-y-3">
                        {loadingTasks ? (
                            <div className="border border-black/[0.06] bg-white rounded-md p-10 text-center text-xs text-black/40">
                                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                                Loading tasks...
                            </div>
                        ) : filteredTasks.length === 0 ? (
                            <div className="border border-black/[0.06] bg-white rounded-md p-10 text-center text-xs text-black/40">
                                Nothing logged here yet.
                            </div>
                        ) : (
                            filteredTasks.map((t) => {
                                const isExpanded = expandedTaskId === t.id;
                                const subtasksCount = t.subtasks?.length || 0;
                                const subtasksCompleted = t.subtasks?.filter((s) => s.completed).length || 0;
                                const commentsCount = t.comments?.length || 0;

                                return (
                                    <div
                                        key={t.id}
                                        className={`border border-black/[0.06] rounded-md transition-all duration-200 overflow-hidden ${
                                            isExpanded ? "bg-white shadow-md border-black/[0.12]" : "bg-white hover:border-black/[0.1] hover:shadow-sm"
                                        }`}
                                    >
                                        {/* Task Primary Header Row */}
                                        <div
                                            onClick={() => setExpandedTaskId(isExpanded ? null : t.id)}
                                            className="px-4 py-3 flex items-center justify-between gap-3 cursor-pointer select-none"
                                        >
                                            <div className="flex items-center gap-2.5 min-w-0 flex-1">
                                                <span
                                                    className={`p-1.5 rounded-sm shrink-0 ${
                                                        t.status === "completed"
                                                            ? "bg-black text-white/50"
                                                            : "bg-black/[0.03] text-black/60"
                                                    }`}
                                                >
                                                    {getCategoryIcon(t.category)}
                                                </span>
                                                <div className="min-w-0 flex-1">
                                                    <p
                                                        className={`text-xs font-semibold truncate ${
                                                            t.status === "completed"
                                                                ? "line-through text-black/40"
                                                                : "text-black/85"
                                                        }`}
                                                    >
                                                        {t.title}
                                                    </p>
                                                    <div className="flex items-center gap-2 mt-0.5 text-[10px] text-black/45 tracking-tight flex-wrap">
                                                        <span className="uppercase font-semibold tracking-wider bg-black/[0.02] border border-black/[0.04] rounded-sm px-1.5 py-0.5 text-black/50">
                                                            {t.category}
                                                        </span>
                                                        <span>•</span>
                                                        <span>Assignee: {getAssigneeName(t.assignee_id)}</span>
                                                        {t.project_id && (
                                                            <>
                                                                <span>•</span>
                                                                <span className="font-medium text-black/60">{getProjectName(t.project_id)}</span>
                                                            </>
                                                        )}
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-2.5 shrink-0">
                                                {/* Mini checklist progress */}
                                                {subtasksCount > 0 && (
                                                    <span className="text-[10px] font-mono text-black/40 bg-black/[0.02] px-1.5 py-0.5 rounded-sm border border-black/[0.04]">
                                                        {subtasksCompleted}/{subtasksCount}
                                                    </span>
                                                )}
                                                {/* Comments count */}
                                                {commentsCount > 0 && (
                                                    <span className="text-[10px] text-black/50 inline-flex items-center gap-1">
                                                        <MessageSquare className="w-3 h-3 text-black/30" />
                                                        {commentsCount}
                                                    </span>
                                                )}
                                                {/* Status indicator */}
                                                <span
                                                    className={`text-[9px] uppercase font-bold px-2 py-0.5 rounded-sm border ${
                                                        t.status === "completed"
                                                            ? "bg-black/[0.02] text-black/40 border-black/[0.06]"
                                                            : t.status === "in_progress"
                                                            ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                                            : "bg-[#c9a961]/10 text-[#9b7b35] border-[#c9a961]/25"
                                                    }`}
                                                >
                                                    {t.status?.replace("_", " ")}
                                                </span>
                                                {isExpanded ? (
                                                    <ChevronUp className="w-3.5 h-3.5 text-black/40" />
                                                ) : (
                                                    <ChevronDown className="w-3.5 h-3.5 text-black/40" />
                                                )}
                                            </div>
                                        </div>

                                        {/* Expanded Operational Workspace details */}
                                        {isExpanded && (
                                            <div className="px-4 pb-4 border-t border-black/[0.04] pt-3 bg-white space-y-4 text-xs">
                                                {/* Description details */}
                                                {t.description && (
                                                    <div className="space-y-1 bg-black/[0.015] border border-black/[0.04] p-2.5 rounded-sm">
                                                        <p className="text-[9px] uppercase font-bold text-black/45 tracking-wider">Details</p>
                                                        <p className="text-black/75 whitespace-pre-wrap leading-relaxed">{t.description}</p>
                                                    </div>
                                                )}

                                                {/* Quick Action Toolbar */}
                                                <div className="flex items-center justify-between gap-3 border-b border-black/[0.04] pb-3 flex-wrap">
                                                    <div className="flex items-center gap-2 flex-wrap">
                                                        <span className="text-[9px] uppercase font-bold text-black/45">Status:</span>
                                                        {["pending", "in_progress", "completed", "archived"].map((st) => (
                                                            <button
                                                                key={st}
                                                                onClick={() => handleUpdateTaskStatus(t.id, st)}
                                                                className={`px-2 py-0.5 text-[10px] tracking-tight font-medium rounded-sm border uppercase transition-all ${
                                                                    t.status === st
                                                                        ? "bg-black text-white border-black"
                                                                        : "text-black/60 hover:text-black hover:bg-black/[0.02] border-black/[0.06]"
                                                                }`}
                                                            >
                                                                {st}
                                                            </button>
                                                        ))}
                                                    </div>
                                                    <div className="flex items-center gap-2 flex-wrap">
                                                        <span className="text-[9px] uppercase font-bold text-black/45">Owner:</span>
                                                        <select
                                                            value={t.assignee_id || ""}
                                                            onChange={(e) => handleUpdateTaskAssignee(t.id, e.target.value)}
                                                            className="text-[11px] border border-black/[0.06] rounded-sm py-0.5 px-2 bg-white focus:outline-none"
                                                        >
                                                            <option value="">Unassigned</option>
                                                            {users.map((u) => (
                                                                <option key={u.id} value={u.id}>
                                                                    {u.name}
                                                                </option>
                                                            ))}
                                                        </select>
                                                        {isAdmin && (
                                                            <button
                                                                onClick={() => handleDeleteTask(t.id)}
                                                                className="p-1 rounded-sm text-black/45 hover:text-red-600 hover:bg-red-50 transition-colors focus:outline-none"
                                                                title="Delete task"
                                                            >
                                                                <Trash2 className="w-3.5 h-3.5" />
                                                            </button>
                                                        )}
                                                    </div>
                                                </div>

                                                {/* Subtasks Checklist */}
                                                <div className="space-y-2 border-b border-black/[0.04] pb-3">
                                                    <p className="text-[10px] uppercase font-bold text-black/45 tracking-wider">Subtasks checklist</p>
                                                    
                                                    {/* Checklist items */}
                                                    {(t.subtasks || []).length > 0 && (
                                                        <div className="space-y-1.5">
                                                            {t.subtasks.map((st) => (
                                                                <div
                                                                    key={st.id}
                                                                    className="flex items-center justify-between gap-3 p-1.5 hover:bg-black/[0.015] rounded-sm"
                                                                >
                                                                    <div className="flex items-center gap-2 min-w-0">
                                                                        <button
                                                                            type="button"
                                                                            onClick={() => handleToggleSubtask(t.id, st.id)}
                                                                            className={`w-4 h-4 border rounded-sm flex items-center justify-center shrink-0 transition-colors focus:outline-none ${
                                                                                st.completed
                                                                                    ? "bg-black border-black text-white"
                                                                                    : "border-black/[0.15] hover:border-black/40"
                                                                            }`}
                                                                        >
                                                                            {st.completed && <Check className="w-3 h-3" />}
                                                                        </button>
                                                                        <span
                                                                            className={`text-xs ${
                                                                                st.completed
                                                                                    ? "line-through text-black/45"
                                                                                    : "text-black/85"
                                                                            }`}
                                                                        >
                                                                            {st.text}
                                                                        </span>
                                                                    </div>
                                                                    <button
                                                                        onClick={() => handleRemoveSubtask(t.id, st.id)}
                                                                        className="p-0.5 text-black/35 hover:text-red-500 hover:bg-red-50 rounded-sm focus:outline-none"
                                                                    >
                                                                        <X className="w-3 h-3" />
                                                                    </button>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    )}

                                                    {/* Checklist Quick Add Inline Input */}
                                                    <div className="flex items-center gap-2">
                                                        <input
                                                            type="text"
                                                            value={subtaskInputs[t.id] || ""}
                                                            onChange={(e) => setSubtaskInputs({ ...subtaskInputs, [t.id]: e.target.value })}
                                                            onKeyDown={(e) => e.key === "Enter" && handleAddSubtask(t.id)}
                                                            placeholder="Add new checklist task..."
                                                            className="flex-1 text-xs px-2 py-1 border border-black/[0.06] rounded-sm focus:outline-none focus:border-black/25"
                                                        />
                                                        <button
                                                            onClick={() => handleAddSubtask(t.id)}
                                                            className="px-2.5 py-1 bg-black text-white hover:bg-black/90 rounded-sm text-[11px] font-semibold"
                                                        >
                                                            Add
                                                        </button>
                                                    </div>
                                                </div>

                                                {/* WhatsApp-like Comments Rail */}
                                                <div className="space-y-3 pt-1">
                                                    <p className="text-[10px] uppercase font-bold text-black/45 tracking-wider">Updates Thread</p>
                                                    
                                                    {/* Comments thread list */}
                                                    {(t.comments || []).length > 0 && (
                                                        <div className="space-y-2 max-h-[160px] overflow-y-auto pr-1">
                                                            {t.comments.map((c) => (
                                                                <div
                                                                    key={c.id}
                                                                    className={`flex flex-col max-w-[85%] rounded-md px-3 py-2 ${
                                                                        c.author_id === currentUserId
                                                                            ? "bg-[#fafaf8] border border-black/[0.05] ml-auto items-end"
                                                                            : "bg-black/[0.015] border border-black/[0.03] mr-auto items-start"
                                                                    }`}
                                                                >
                                                                    <div className="flex items-center gap-1.5 mb-0.5 text-[9px] text-black/35 font-bold tracking-tight">
                                                                        <span>{c.author_name}</span>
                                                                        <span>·</span>
                                                                        <span>{new Date(c.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                                                                    </div>
                                                                    <p className="text-[11px] text-black/85 font-medium whitespace-pre-wrap leading-normal break-words w-full">{c.text}</p>
                                                                    
                                                                    {/* Render comment attachments */}
                                                                    {(c.attachments || []).map((att, idx) => (
                                                                        <div key={idx} className="mt-1 w-full border-t border-black/[0.04] pt-1">
                                                                            {att.type === "image" ? (
                                                                                <a href={att.url} target="_blank" rel="noreferrer" className="block mt-0.5">
                                                                                    <img src={att.url} alt="Attachment" className="max-h-[80px] rounded-sm object-cover border border-black/[0.06] hover:opacity-90" />
                                                                                </a>
                                                                            ) : (
                                                                                <a href={att.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[10px] text-blue-600 hover:underline mt-0.5 break-all">
                                                                                    <Link2 className="w-2.5 h-2.5 shrink-0" />
                                                                                    {att.url}
                                                                                </a>
                                                                            )}
                                                                        </div>
                                                                    ))}
                                                                </div>
                                                            ))}
                                                        </div>
                                                    )}

                                                    {/* Reply Stream Input */}
                                                    <div className="space-y-1.5 pt-1">
                                                        <div className="flex items-center gap-2">
                                                            <input
                                                                type="text"
                                                                value={commentInputs[t.id] || ""}
                                                                onChange={(e) => setCommentInputs({ ...commentInputs, [t.id]: e.target.value })}
                                                                onKeyDown={(e) => e.key === "Enter" && handleAddComment(t.id)}
                                                                placeholder="Type a team update..."
                                                                className="flex-1 text-xs px-2.5 py-1.5 border border-black/[0.06] rounded-sm focus:outline-none focus:border-black/25"
                                                            />
                                                            <button
                                                                onClick={() => handleAddComment(t.id)}
                                                                className="p-1.5 bg-black text-white hover:bg-black/90 rounded-sm focus:outline-none"
                                                            >
                                                                <Send className="w-3.5 h-3.5" />
                                                            </button>
                                                        </div>

                                                        {/* Optional attachments inputs */}
                                                        <div className="flex items-center gap-2 bg-black/[0.015] border border-black/[0.04] p-1.5 rounded-sm">
                                                            <span className="text-[9px] uppercase font-bold text-black/45 tracking-wider shrink-0 mr-1">Attachment:</span>
                                                            <select
                                                                value={commentAttachmentType[t.id] || "link"}
                                                                onChange={(e) => setCommentAttachmentType({ ...commentAttachmentType, [t.id]: e.target.value })}
                                                                className="text-[10px] border border-black/[0.06] rounded-sm bg-white py-0.5 px-1 focus:outline-none"
                                                            >
                                                                <option value="link">Link Link</option>
                                                                <option value="image">Image URL</option>
                                                            </select>
                                                            <input
                                                                type="text"
                                                                value={commentAttachmentUrl[t.id] || ""}
                                                                onChange={(e) => setCommentAttachmentUrl({ ...commentAttachmentUrl, [t.id]: e.target.value })}
                                                                placeholder="Paste URL..."
                                                                className="flex-1 text-[10px] px-1.5 py-0.5 border border-black/[0.06] rounded-sm bg-white focus:outline-none"
                                                            />
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                );
                            })
                        )}
                    </div>
                </div>

                {/* 3. Right Column: Scouting Pipeline Roster (5 Columns) */}
                <div className="lg:col-span-5 space-y-4">
                    
                    {/* Fast entry bar */}
                    <div className="border border-black/[0.06] bg-white rounded-md p-4 space-y-3 shadow-sm">
                        <p className="text-xs font-semibold uppercase tracking-wider text-black/85">Ultra-Fast Scouting Log</p>
                        <form onSubmit={handleCreateScout} className="space-y-2">
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold text-black/45">Instagram Profile Link</label>
                                <div className="relative">
                                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-black/35">
                                        <Instagram className="w-3.5 h-3.5" />
                                    </span>
                                    <input
                                        type="url"
                                        value={newScout.instagram_link}
                                        onChange={(e) => setNewScout({ ...newScout, instagram_link: e.target.value })}
                                        placeholder="https://instagram.com/profile..."
                                        className="w-full text-xs pl-8 pr-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30"
                                        required
                                    />
                                </div>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                                <div className="space-y-1">
                                    <label className="text-[10px] uppercase font-bold text-black/45">Phone Number</label>
                                    <div className="relative">
                                        <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-black/35">
                                            <Phone className="w-3.5 h-3.5" />
                                        </span>
                                        <input
                                            type="text"
                                            value={newScout.phone}
                                            onChange={(e) => setNewScout({ ...newScout, phone: e.target.value })}
                                            placeholder="+91..."
                                            className="w-full text-xs pl-8 pr-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none focus:border-black/30"
                                            required
                                        />
                                    </div>
                                </div>
                                <div className="space-y-1">
                                    <label className="text-[10px] uppercase font-bold text-black/45">Scout Name</label>
                                    <div className="relative">
                                        <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-black/35">
                                            <User className="w-3.5 h-3.5" />
                                        </span>
                                        <input
                                            type="text"
                                            value={newScout.name}
                                            onChange={(e) => setNewScout({ ...newScout, name: e.target.value })}
                                            placeholder="Optional name..."
                                            className="w-full text-xs pl-8 pr-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none"
                                        />
                                    </div>
                                </div>
                            </div>
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold text-black/45">Scouting Notes</label>
                                <input
                                    type="text"
                                    value={newScout.notes}
                                    onChange={(e) => setNewScout({ ...newScout, notes: e.target.value })}
                                    placeholder="Add inline scouting context / looks..."
                                    className="w-full text-xs px-2.5 py-1.5 border border-black/[0.08] rounded-sm focus:outline-none"
                                />
                            </div>
                            <button
                                type="submit"
                                className="w-full bg-black text-white hover:bg-black/95 py-2 rounded-sm text-xs font-semibold uppercase tracking-wider mt-1 focus:outline-none"
                            >
                                + Log Scout
                            </button>
                        </form>
                    </div>

                    {/* Scouting Pipeline Database List */}
                    <div className="space-y-2">
                        <p className="text-[10px] uppercase font-bold text-black/45 tracking-wider px-1">Active Scouting Queue ({scouts.length})</p>
                        
                        {loadingScouts ? (
                            <div className="border border-black/[0.06] bg-white rounded-md p-6 text-center text-xs text-black/40">
                                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                                Loading queue...
                            </div>
                        ) : scouts.length === 0 ? (
                            <div className="border border-black/[0.06] bg-white rounded-md p-6 text-center text-xs text-black/40">
                                Scouting pipeline is empty.
                            </div>
                        ) : (
                            scouts.map((s) => (
                                <div
                                    key={s.id}
                                    className="border border-black/[0.06] bg-white rounded-md p-3.5 space-y-2.5 shadow-sm"
                                >
                                    {/* Scout primary details */}
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="min-w-0 flex-1">
                                            <div className="flex items-center gap-1.5">
                                                {s.name && <span className="text-xs font-semibold text-black/85 truncate">{s.name}</span>}
                                                <a
                                                    href={s.instagram_link}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="text-[11px] text-blue-600 hover:underline inline-flex items-center gap-0.5 truncate"
                                                >
                                                    <Instagram className="w-3 h-3 text-black/40 shrink-0" />
                                                    Instagram Profile
                                                </a>
                                            </div>
                                            <div className="text-[10px] text-black/50 font-mono mt-0.5">
                                                Phone: {s.phone}
                                            </div>
                                        </div>
                                        {isAdmin && (
                                            <button
                                                onClick={() => handleDeleteScout(s.id)}
                                                className="p-1 text-black/35 hover:text-red-500 hover:bg-red-50 rounded-sm focus:outline-none"
                                            >
                                                <Trash2 className="w-3.5 h-3.5" />
                                            </button>
                                        )}
                                    </div>

                                    {/* Notes block editable inline */}
                                    <div className="space-y-0.5">
                                        <label className="text-[9px] uppercase font-bold text-black/40">Scouting Notes</label>
                                        <input
                                            type="text"
                                            defaultValue={s.notes || ""}
                                            onBlur={(e) => handleUpdateScoutNotes(s.id, e.target.value)}
                                            placeholder="Click to write scout update..."
                                            className="w-full text-xs border border-transparent hover:border-black/[0.06] focus:border-black/25 focus:bg-white rounded-sm px-1.5 py-0.5 text-black/75 bg-black/[0.015] focus:outline-none"
                                        />
                                    </div>

                                    {/* Quick Status pills */}
                                    <div className="space-y-1">
                                        <label className="text-[9px] uppercase font-bold text-black/40">Pipeline Status</label>
                                        <div className="flex flex-wrap gap-1">
                                            {[
                                                { id: "not_contacted", label: "New" },
                                                { id: "reached_out", label: "Contacted" },
                                                { id: "replied", label: "Replied" },
                                                { id: "added_to_group", label: "Grouped" },
                                                { id: "added_to_database", label: "DB Approved" },
                                                { id: "ignored", label: "Ignored" },
                                            ].map((st) => (
                                                <button
                                                    key={st.id}
                                                    onClick={() => handleUpdateScoutStatus(s.id, st.id)}
                                                    className={`px-2 py-0.5 text-[9px] font-medium tracking-tight rounded-sm border uppercase transition-all ${
                                                        s.status === st.id
                                                            ? "bg-black text-white border-black"
                                                            : "bg-black/[0.015] text-black/50 border-black/[0.04] hover:bg-black/[0.03]"
                                                    }`}
                                                >
                                                    {st.label}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                </div>

            </div>
        </div>
    );
}
