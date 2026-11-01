
async function checkBackend() {
    const statusElement = document.getElementById("system-status");

    try {
        const response = await fetch("/api/health");

        if (!response.ok) {
            throw new Error("Backend returned an error");
        }

        const data = await response.json();

        if (data.success && data.status === "running") {
            statusElement.textContent = "Backend connected";
            statusElement.classList.add("online");
            statusElement.classList.remove("offline");
        } else {
            throw new Error("Unexpected backend response");
        }
    } catch (error) {
        statusElement.textContent = "Backend unavailable";
        statusElement.classList.add("offline");
        statusElement.classList.remove("online");

        console.error("Backend health check failed:", error);
    }
}

document.addEventListener("DOMContentLoaded", checkBackend);
