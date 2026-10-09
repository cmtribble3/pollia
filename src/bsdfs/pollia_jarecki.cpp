#include <mitsuba/core/properties.h>
#include <mitsuba/core/warp.h>
#include <mitsuba/render/bsdf.h>
#include <mitsuba/render/texture.h>
#include <mitsuba/render/pollia_jarecki.h>
#include <cmath>

NAMESPACE_BEGIN(mitsuba)

/**
 * PolRAY's Jarecki polarization transform with a separate Lambertian scalar
 * BRDF. Parameters accept scalar values or complete RGB triplets, retained
 * as runtime data for shared compiled code across materials.
 */
template <typename Float, typename Spectrum>
class PolliaJarecki final : public BSDF<Float, Spectrum> {
public:
    MI_IMPORT_BASE(BSDF, m_flags, m_components)
    MI_IMPORT_TYPES(Texture)

    PolliaJarecki(const Properties &props) : Base(props) {
        m_reflectance = props.get_texture<Texture>("reflectance", .5f);
        m_a = get_channels(props, "a", .26f);
        m_b = get_channels(props, "b", 2.f);
        m_n_real = get_channels(props, "n_real", 1.26f);
        m_n_imag = get_channels(props, "n_imag", .08f);
        m_xi0_max = get_channels(props, "xi0_max", .26f);
        for (size_t c = 0; c < 3; ++c) {
            if (!std::isfinite(m_a[c]) || !std::isfinite(m_b[c]) ||
                !std::isfinite(m_n_real[c]) || !std::isfinite(m_n_imag[c]) ||
                !std::isfinite(m_xi0_max[c]) || m_n_real[c] <= 0.f ||
                m_n_imag[c] < 0.f)
                Throw("pollia_jarecki: invalid material parameters");
            // Runtime data lets Dr.Jit share compiled BSDF code across materials.
            m_parameters[c] = pollia::JareckiParams<Float>(
                dr::opaque<Float>(m_a[c]), dr::opaque<Float>(m_b[c]),
                dr::opaque<Float>(m_n_real[c]), dr::opaque<Float>(m_n_imag[c]),
                dr::opaque<Float>(m_xi0_max[c]));
        }
        m_flags = BSDFFlags::DiffuseReflection | BSDFFlags::FrontSide;
        m_components.push_back(m_flags);
    }

    ScalarColor3f get_channels(const Properties &props, std::string_view name,
                               ScalarFloat default_value) {
        if (props.has_property(name) && props.type(name) == Properties::Type::Color) {
            if constexpr (!is_rgb_v<Spectrum>)
                Throw("pollia_jarecki: per-channel %s requires an RGB variant", name);
            return props.get<ScalarColor3f>(name);
        }
        return ScalarColor3f(props.get<ScalarFloat>(name, default_value));
    }

    void traverse(TraversalCallback *cb) override {
        cb->put("reflectance", m_reflectance, ParamFlags::Differentiable);
    }

    std::pair<BSDFSample3f, Spectrum> sample(const BSDFContext &ctx,
                                             const SurfaceInteraction3f &si,
                                             Float /* sample1 */,
                                             const Point2f &sample2,
                                             Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFSample, active);
        BSDFSample3f bs = dr::zeros<BSDFSample3f>();
        active &= Frame3f::cos_theta(si.wi) > 0.f;
        if (unlikely(dr::none_or<false>(active) ||
                     !ctx.is_enabled(BSDFFlags::DiffuseReflection)))
            return { bs, 0.f };

        bs.wo = warp::square_to_cosine_hemisphere(sample2);
        bs.pdf = warp::square_to_cosine_hemisphere_pdf(bs.wo);
        bs.eta = 1.f;
        bs.sampled_type = +BSDFFlags::DiffuseReflection;
        bs.sampled_component = 0;
        Spectrum value = eval(ctx, si, bs.wo, active);
        return { bs, dr::select(active && bs.pdf > 0.f, value / bs.pdf, 0.f) };
    }

    Spectrum eval(const BSDFContext &ctx, const SurfaceInteraction3f &si,
                  const Vector3f &wo, Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFEvaluate, active);
        if (!ctx.is_enabled(BSDFFlags::DiffuseReflection))
            return 0.f;
        Float cos_i = Frame3f::cos_theta(si.wi),
              cos_o = Frame3f::cos_theta(wo);
        active &= cos_i > 0.f && cos_o > 0.f;
        UnpolarizedSpectrum scalar = m_reflectance->eval(si, active) *
                                     dr::InvPi<Float> * cos_o;

        if constexpr (is_polarized_v<Spectrum>) {
            // si.wi points toward the camera in radiance transport; wo points
            // toward the source. The helper uses that reverse-path ordering.
            Vector3f wi_light = ctx.mode == TransportMode::Radiance ? wo : si.wi;
            Vector3f wo_camera = ctx.mode == TransportMode::Radiance ? si.wi : wo;
            Vector3f n(0.f, 0.f, 1.f);
            Spectrum result = dr::zeros<Spectrum>();
            for (size_t c = 0; c < dr::size_v<UnpolarizedSpectrum>; ++c) {
                auto local = pollia::evaluate_jarecki(
                    wi_light, wo_camera, n, m_parameters[is_rgb_v<Spectrum> ? c : 0]);
                auto j = pollia::world_jones(local, wi_light, wo_camera);
                auto m = pollia::mixture_mueller(j, local.gamma);
                for (int i = 0; i < 4; ++i)
                    for (int k = 0; k < 4; ++k)
                        result(i,k)[c] = dr::select(active && local.valid,
                                                   m(i,k) * scalar[c], 0.f);
            }
            return result;
        } else {
            return scalar & active;
        }
    }

    Float pdf(const BSDFContext &ctx, const SurfaceInteraction3f &si,
              const Vector3f &wo, Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFEvaluate, active);
        if (!ctx.is_enabled(BSDFFlags::DiffuseReflection))
            return 0.f;
        active &= Frame3f::cos_theta(si.wi) > 0.f && Frame3f::cos_theta(wo) > 0.f;
        return dr::select(active, warp::square_to_cosine_hemisphere_pdf(wo), 0.f);
    }

    std::pair<Spectrum, Float> eval_pdf(const BSDFContext &ctx,
                                        const SurfaceInteraction3f &si,
                                        const Vector3f &wo,
                                        Mask active) const override {
        return { eval(ctx, si, wo, active), pdf(ctx, si, wo, active) };
    }

    Mask has_attribute(const std::string &name, Mask active) const override {
        if (name == "pollia_jarecki_params" || name == "pollia_optical_constants" ||
            name == "pollia_reflectance" ||
            name == "pollia_jarecki_params_0" || name == "pollia_jarecki_params_1" ||
            name == "pollia_jarecki_params_2" || name == "pollia_optical_constants_0" ||
            name == "pollia_optical_constants_1" || name == "pollia_optical_constants_2")
            return active;
        return Base::has_attribute(name, active);
    }

    UnpolarizedSpectrum eval_attribute(const std::string &name,
                                        const SurfaceInteraction3f &si,
                                        Mask active) const override {
        if (name == "pollia_reflectance")
            return m_reflectance->eval(si, active);
        return Base::eval_attribute(name, si, active);
    }

    Float eval_attribute_1(const std::string &name,
                           const SurfaceInteraction3f &si,
                           Mask active) const override {
        if (name == "pollia_reflectance")
            return m_reflectance->eval_1(si, active);
        return Base::eval_attribute_1(name, si, active);
    }

    Color3f eval_attribute_3(const std::string &name,
                             const SurfaceInteraction3f &si,
                             Mask active) const override {
        for (size_t c = 0; c < 3; ++c) {
            const std::string suffix = "_" + std::to_string(c);
            const auto &p = m_parameters[c];
            if (name == "pollia_jarecki_params" + suffix ||
                (c == 0 && name == "pollia_jarecki_params"))
                return Color3f(p[0], p[1], p[4]) & active;
            if (name == "pollia_optical_constants" + suffix ||
                (c == 0 && name == "pollia_optical_constants"))
                return Color3f(p[2], p[3], Float(0.f)) & active;
        }
        return Base::eval_attribute_3(name, si, active);
    }

    BSDFFeatures3f eval_features(const SurfaceInteraction3f &si,
                                 Mask active) const override {
        return { m_reflectance->eval(si, active), 0.f, 0.f, si.sh_frame, 1.f };
    }

    std::string to_string() const override {
        std::ostringstream oss;
        oss << "PolliaJarecki[reflectance=" << string::indent(m_reflectance)
            << ", a=" << m_a << ", b=" << m_b << ", n=(" << m_n_real
            << ", " << m_n_imag << "), xi0_max=" << m_xi0_max << "]";
        return oss.str();
    }

    MI_DECLARE_CLASS(PolliaJarecki)
private:
    ref<Texture> m_reflectance;
    ScalarColor3f m_a, m_b, m_n_real, m_n_imag, m_xi0_max;
    pollia::JareckiParams<Float> m_parameters[3];
    MI_TRAVERSE_CB(Base, m_reflectance)
};

MI_EXPORT_PLUGIN(PolliaJarecki)
NAMESPACE_END(mitsuba)
