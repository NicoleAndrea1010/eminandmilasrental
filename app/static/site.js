const menuButton = document.querySelector(".menu-toggle");
const navLinks = document.querySelector(".nav-links");

const setMenuOpen = (isOpen) => {
  if (!menuButton || !navLinks) return;
  menuButton.setAttribute("aria-expanded", String(isOpen));
  menuButton.setAttribute("aria-label", isOpen ? "Close menu" : "Open menu");
  const label = menuButton.querySelector("[data-menu-label]");
  if (label) label.textContent = isOpen ? "Close" : "Menu";
  navLinks.classList.toggle("is-open", isOpen);
};

menuButton?.addEventListener("click", () => {
  setMenuOpen(menuButton.getAttribute("aria-expanded") !== "true");
});
navLinks?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", () => setMenuOpen(false));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && menuButton?.getAttribute("aria-expanded") === "true") {
    setMenuOpen(false);
    menuButton.focus();
  }
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 700) setMenuOpen(false);
});

document.querySelectorAll("[data-open-modal]").forEach((button) => {
  button.addEventListener("click", () => {
    const modal = document.querySelector(`[data-modal="${button.dataset.openModal}"]`);
    if (modal instanceof HTMLDialogElement) modal.showModal();
  });
});

document.querySelectorAll("[data-close-modal]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog")?.close());
});

document.querySelectorAll("dialog[data-modal]").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

const getFieldError = (field) => {
  if (field.validity.valueMissing) {
    return field.type === "checkbox" ? "Please confirm to continue." : "This field is required.";
  }
  if (field.validity.typeMismatch && field.type === "email") return "Enter a valid email address.";
  if (field.validity.patternMismatch) return field.title || "Use the requested format.";
  if (field.validity.tooShort) return `Enter at least ${field.minLength} characters.`;
  if (field.validity.tooLong) return `Keep this to ${field.maxLength} characters or fewer.`;
  if (field.validity.rangeUnderflow) return `Enter a value of ${field.min} or greater.`;
  if (field.validity.rangeOverflow) return `Enter a value no greater than ${field.max}.`;
  if (field.validity.stepMismatch) return "Enter a value using the requested increments.";
  if (field.validity.badInput) return "Enter a valid value.";
  return "Please check this field.";
};

document.querySelectorAll("[data-normalize-uppercase]").forEach((field) => {
  field.addEventListener("blur", () => {
    field.value = field.value.replace(/\s+/g, " ").trim().toUpperCase();
  });
});

document.querySelectorAll("[data-normalize-email]").forEach((field) => {
  field.addEventListener("blur", () => {
    field.value = field.value.trim().toLowerCase();
  });
});

document.querySelectorAll("[data-normalize-phone]").forEach((field) => {
  field.addEventListener("blur", () => {
    field.value = field.value.trim().replace(/[\s()-]/g, "");
  });
});

document.querySelectorAll("[data-normalize-whitespace]").forEach((field) => {
  field.addEventListener("blur", () => {
    field.value = field.value.replace(/\s+/g, " ").trim();
  });
});

document.querySelectorAll("form").forEach((form, formIndex) => {
  const fields = form.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea'
  );
  fields.forEach((field, fieldIndex) => {
    if (!field.id) field.id = `form-${formIndex}-field-${fieldIndex}`;
    const error = document.createElement("span");
    error.className = "field-error";
    error.id = `${field.id}-error`;
    error.hidden = true;
    field.closest("label")?.append(error);
    const describedBy = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    describedBy.add(error.id);
    field.setAttribute("aria-describedby", [...describedBy].join(" "));

    const updateField = (show) => {
      if (show) field.dataset.touched = "true";
      if (field.dataset.touched !== "true") return;
      const invalid = !field.validity.valid;
      field.setAttribute("aria-invalid", String(invalid));
      error.textContent = invalid ? getFieldError(field) : "";
      error.hidden = !invalid;
    };

    field.addEventListener("blur", () => updateField(true));
    field.addEventListener("input", () => updateField(false));
    field.addEventListener("change", () => updateField(false));
    form.addEventListener("invalid", (event) => {
      if (event.target === field) updateField(true);
    }, true);
    form.addEventListener("submit", () => updateField(true));
  });
});

const requestedModal = document.body.dataset.activeModal;
if (requestedModal) {
  const modal = document.querySelector(`[data-modal="${requestedModal}"]`);
  if (modal instanceof HTMLDialogElement) modal.showModal();
}

const mainPhoto = document.querySelector("[data-main-photo]");
document.querySelectorAll("[data-gallery-image]").forEach((button) => {
  button.addEventListener("click", () => {
    if (mainPhoto instanceof HTMLImageElement) {
      mainPhoto.classList.add("is-changing");
      mainPhoto.src = button.dataset.galleryImage;
      document.querySelectorAll("[data-gallery-image]").forEach((thumbnail) => {
        thumbnail.setAttribute("aria-pressed", String(thumbnail === button));
      });
      mainPhoto.addEventListener("load", () => mainPhoto.classList.remove("is-changing"), { once: true });
    }
  });
});

const revealItems = document.querySelectorAll("[data-reveal]");
const canAnimateReveals = "IntersectionObserver" in window
  && revealItems.length
  && !document.hidden
  && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let revealObserver = null;
if (canAnimateReveals) {
  document.documentElement.classList.add("has-motion");
  revealObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -48px 0px", threshold: 0.08 });
  revealItems.forEach((item) => revealObserver.observe(item));
} else {
  revealItems.forEach((item) => item.classList.add("is-visible"));
}

const revealAppendedItems = (items) => {
  items.forEach((item) => {
    if (revealObserver) revealObserver.observe(item);
    else item.classList.add("is-visible");
  });
};

const listingGrid = document.querySelector("[data-property-grid]");
const listingPagination = document.querySelector("[data-listing-pagination]");
const listingNextLink = listingPagination?.querySelector("[data-listing-next]");
const listingStatus = listingPagination?.querySelector("[data-listing-status]");
const resultsCount = document.querySelector("[data-results-count]");

if (listingGrid && listingPagination && listingNextLink && listingStatus && resultsCount) {
  let loadingMore = false;
  let pauseAutoLoad = false;
  const firstResult = Number(resultsCount.dataset.firstResult);
  const listingScrollHint = listingPagination.querySelector(".listing-scroll-hint");

  const loadMoreListings = async () => {
    if (loadingMore) return;
    const requestUrl = new URL(listingNextLink.href, window.location.href);
    if (requestUrl.origin !== window.location.origin || requestUrl.pathname !== window.location.pathname) {
      listingStatus.textContent = "More homes could not be loaded. Please try again.";
      pauseAutoLoad = true;
      return;
    }

    loadingMore = true;
    listingNextLink.setAttribute("aria-disabled", "true");
    listingStatus.textContent = "Loading more homes.";

    try {
      const response = await fetch(requestUrl, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) throw new Error(`Request failed with status ${response.status}.`);

      const lastResult = Number(response.headers.get("X-Last-Result"));
      const hasNextPage = response.headers.get("X-Has-Next") === "true";
      const nextPath = response.headers.get("X-Next-Page");
      let nextUrl = null;
      if (hasNextPage) {
        if (!nextPath) throw new Error("The next page link is missing.");
        nextUrl = new URL(nextPath, window.location.href);
        if (nextUrl.origin !== window.location.origin || nextUrl.pathname !== window.location.pathname) {
          throw new Error("The next page link is invalid.");
        }
      }
      if (!Number.isSafeInteger(lastResult) || lastResult < firstResult) {
        throw new Error("The listing count is invalid.");
      }

      const template = document.createElement("template");
      template.innerHTML = await response.text();
      const newCards = Array.from(template.content.querySelectorAll(".property-card"));
      if (!newCards.length) throw new Error("No property cards were returned.");

      listingGrid.append(...newCards);
      resultsCount.dataset.lastResult = String(lastResult);
      resultsCount.textContent = `Showing ${firstResult}–${lastResult} properties${hasNextPage ? " · more available" : ""}`;
      if (listingScrollHint) {
        listingScrollHint.textContent = hasNextPage ? "Scroll to discover more homes" : "You’re all caught up";
      }
      revealAppendedItems(newCards);

      if (hasNextPage && nextUrl) {
        listingNextLink.href = nextUrl.href;
        listingStatus.textContent = `Loaded ${newCards.length} more homes.`;
      } else {
        listingNextLink.remove();
        listingStatus.textContent = "All available homes have been loaded.";
      }
      pauseAutoLoad = false;
    } catch (error) {
      console.error("Could not load additional property listings.", error);
      listingStatus.textContent = "More homes could not be loaded. Select Load more homes to try again.";
      pauseAutoLoad = true;
    } finally {
      loadingMore = false;
      if (listingNextLink.isConnected) listingNextLink.removeAttribute("aria-disabled");
      checkForMoreListings();
    }
  };

  const checkForMoreListings = () => {
    if (pauseAutoLoad || loadingMore || !listingNextLink.isConnected) return;
    if (listingNextLink.getBoundingClientRect().top <= window.innerHeight + 600) {
      loadMoreListings();
    }
  };

  listingNextLink.addEventListener("click", (event) => {
    event.preventDefault();
    pauseAutoLoad = false;
    loadMoreListings();
  });
  window.addEventListener("scroll", checkForMoreListings, { passive: true });
  window.addEventListener("resize", checkForMoreListings);
  checkForMoreListings();
}

const slideImage = document.querySelector("[data-slide-image]");
if (slideImage instanceof HTMLImageElement) {
  const slides = [
    ["photo3.jpg", "Find a home that feels right"],
    ["photo1.jpg", "A bright kitchenette and living area"],
    ["photo2.jpg", "A comfortable living room"],
    ["photo4.jpg", "A welcoming entry and stairwell"],
    ["photo7.jpg", "An open loft-style interior"],
    ["photo5.jpg", "A furnished living space"],
    ["photo6.jpg", "An interior staircase"],
  ];
  const counter = document.querySelector("[data-slide-count]");
  const carousel = slideImage.closest("[data-carousel]");
  let current = 0;
  let slideTimer;
  const showSlide = (index) => {
    current = (index + slides.length) % slides.length;
    const [image] = slides[current];
    slideImage.classList.add("is-changing");
    const finishTransition = () => slideImage.classList.remove("is-changing");
    slideImage.addEventListener("load", finishTransition, { once: true });
    slideImage.addEventListener("error", finishTransition, { once: true });
    slideImage.src = `/static/images/${image}`;
    if (counter) counter.textContent = `${String(current + 1).padStart(2, "0")} / ${String(slides.length).padStart(2, "0")}`;
  };
  document.querySelector("[data-slide-prev]")?.addEventListener("click", () => showSlide(current - 1));
  document.querySelector("[data-slide-next]")?.addEventListener("click", () => showSlide(current + 1));
  const stopSlides = () => window.clearInterval(slideTimer);
  const startSlides = () => {
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches && !document.hidden) {
      stopSlides();
      slideTimer = window.setInterval(() => showSlide(current + 1), 7000);
    } else {
      stopSlides();
    }
  };
  carousel?.addEventListener("mouseenter", stopSlides);
  carousel?.addEventListener("mouseleave", startSlides);
  carousel?.addEventListener("focusin", stopSlides);
  carousel?.addEventListener("focusout", (event) => {
    if (!carousel.contains(event.relatedTarget)) startSlides();
  });
  document.addEventListener("visibilitychange", startSlides);
  startSlides();
}
