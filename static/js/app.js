// =========================================
// PUREWASH SMART LOCKER
// app.js
// =========================================

const form = document.getElementById("dropoffForm");

const loadingScreen = document.getElementById("loadingScreen");
const successScreen = document.getElementById("successScreen");

const submitBtn = document.getElementById("submitBtn");

form.addEventListener("submit", async function (e) {

    e.preventDefault();

    const name = document.getElementById("name").value.trim();
    const phone = document.getElementById("phone").value.trim();
    const locker_id = document.getElementById("locker_id").value;

    if (name.length < 2) {
        alert("Please enter your full name.");
        return;
    }

    if (!/^[0-9]{10}$/.test(phone)) {
        alert("Please enter a valid 10-digit WhatsApp number.");
        return;
    }

    submitBtn.disabled = true;
    submitBtn.innerText = "Opening...";

    form.style.display = "none";
    loadingScreen.classList.remove("hidden");

    try {

        const response = await fetch("/api/dropoff", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify({

                locker_id: locker_id,
                name: name,
                phone: phone

            })

        });

        const data = await response.json();

        if (data.ok) {

            setTimeout(() => {

                loadingScreen.classList.add("hidden");

                successScreen.classList.remove("hidden");

            }, 2200);

        } else {

            throw new Error(data.error || "Something went wrong.");

        }

    } catch (err) {

        loadingScreen.classList.add("hidden");

        form.style.display = "block";

        submitBtn.disabled = false;
        submitBtn.innerText = "Open Compartment";

        alert(err.message);

    }

});
